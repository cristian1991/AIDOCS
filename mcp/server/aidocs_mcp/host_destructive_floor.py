"""HOST DESTRUCTIVE SCOPE FLOOR (#1135, incident 2026-10-04).

WHAT HAPPENED. An agent ran a recursive delete of a repository under
``D:\\tmp`` from a working directory AIDOCS does not manage. Every layer that
could have refused it was reachable only AFTER project detection, so a
non-managed cwd received no verdict at all, and the operator runs with host
permission prompts bypassed. Nothing stood between the agent and the disk.

WHAT THIS IS. The one check that runs for EVERY PreToolUse shell / file
mutation call, in managed and non-managed directories alike, BEFORE the
broker, project detection, the index database, settings, identity, or any
heavy import. It is executed by the stdlib-only launcher shim
(``claude_hook_shim.main``) straight after the payload is read, and again at
the top of ``claude_hook.main`` for the module-form hook.

THE LAW (reviewer r0b2, d907b970-0cf):

* A KNOWN DESTRUCTIVE PRIMITIVE whose target is not provably a STRICT
  descendant of the Claude Code SESSION SCRATCHPAD is DENIED. Terminal class
  ``host_destructive_scope_floor``: never ask, never confirmable, never
  remembered, and NOTHING lifts it -- this module reads no project file, no
  operator setting, no scratch declaration, no prompt history and no session
  state. Its only inputs are the payload and the process environment (TEMP
  and home, to locate the scratchpad and home).
* PATH LAW: relative targets resolve against the payload cwd; ``..`` is
  normalised; symlinks / junctions / reparse points are resolved (the
  existing parent is canonicalised for a target that does not exist yet);
  containment is decided with ``os.path.commonpath``, never a string prefix.
  The scratch root itself, a drive root, the home directory, the cwd and
  every ancestor of those are never in scope. A glob, ``$VAR``, backtick or
  ``$()`` in a target cannot be bounded and is denied.
* SCRATCH IS STRUCTURAL, NEVER DECLARED (r0b2 architectural ruling). The
  only scratch authority is Claude Code's session scratchpad
  (``<TEMP>/claude/<project>/<session>/scratchpad``), strict children only.
  There is NO declaration file: an earlier ``~/.aidocs/host_scratch_roots.json``
  was writable by the same OS user as the agent, so no amount of command-
  syntax write protection could make it an integrity boundary, and it was
  removed. A file of that name (or any other) widens nothing. Custom roots
  would need a separate operator-controlled trust mechanism whose integrity
  lives outside agent-writable state -- out of scope here. A lexical
  ``/tmp/`` spelling is NOT scratch either.

WHAT THIS FLOOR IS -- AND IS NOT. It refuses KNOWN, OBSERVABLE destructive
shapes: the primitives, carriers, inline code and script files it can read.
It does not model arbitrary program semantics. Opaque carriers can still
hide a destructive primitive -- ``python -m <module>``, ``npm`` / ``bun`` /
``pnpm`` / ``yarn run`` (package scripts), ``make`` targets, compiled
binaries, paths assembled from pieces at run time. Since the scratch
authority is structural, none of them can WIDEN what the floor permits; they
can only act outside its sight, which is the residual scope.
* Wrapper peeling is shared with the judge (``shell_wrapper_peel``) and
  extended here with the arbitrary-payload carriers (``sh -c``, ``cmd /c``,
  ``pwsh -Command`` / ``-EncodedCommand``, ``wsl``, ``docker run|exec``,
  ``find -exec``, ``xargs``, inline python / node). Nesting is capped; a
  carrier whose payload cannot be parsed, or that nests past the cap, is
  denied.

THE FLOOR'S OWN FILES ARE NOT SHELL-MUTABLE (r0b2 review). The host-
control set -- the deployed shim and floor companions, ``~/.aidocs`` runtime files, Claude Code settings / hooks /
plugins (``.claude/worktrees/`` and ``.claude/projects/*/memory/`` excluded)
-- is hard-denied as the TARGET of every mutation carrier the floor
recognises: output redirects, tee, cp/mv/install/rsync/dd/ln/sed -i and any
other non-read-only command naming it, PowerShell Set-Content / Out-File /
Copy-Item / .NET File.*, cmd ``>`` / copy, and inline python / node / .NET
writes. Editing ``.claude/settings.json`` (or any other Claude Code control
file) is therefore deliberately OPERATOR-ONLY: an agent cannot do it through
Edit/Write or the shell, which also means the update-config skill cannot
change settings on the agent's behalf.

SCRIPT FILES are read (bounded, ``SCRIPT_MAX_BYTES``) and analysed with the
same shape logic; a script carrier that cannot be inspected is denied.

WHY STDLIB-ONLY AND LOADED BY PATH. The shim lives in ``~/.aidocs/runtime``
outside site-packages so a package swap cannot take it away (#616). This
module is deployed verbatim beside it (with its two companions) and loaded by
file path, so it may not use a relative import or import the package. Its
companions are loaded the same way.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import os
import re
import sys
from pathlib import Path

#: The ONE platform fact this module reads. DETECTION NEVER BRANCHES ON IT
#: (#1135 follow-up, VPS gate 2b): a destructive primitive from EITHER family
#: -- POSIX or Windows -- is recognised on every host. It only decides how a
#: host path is SPELLED (Git Bash `/c/...`, WSL `/mnt/c/...`, which env var
#: names TEMP / home), i.e. what can be proven scratch -- never what is
#: destructive. Tests force both values to prove that.
_WINDOWS = os.name == "nt"

FLOOR_CLASS = "host_destructive_scope_floor"
EXCEPTION_CLASS = "hook_exception_fail_closed"

#: Carrier / wrapper nesting the analysis will follow before refusing.
MAX_DEPTH = 8
#: Total script analyses per call. The floor must answer FAST (a host hook
#: timeout reads as "proceed"), so a pathological command is refused rather
#: than analysed without bound.
MAX_ANALYSES = 400

# ── tool classes (pinned; tests hold them against every installed matcher) ──
HOST_SHELL_TOOLS: frozenset[str] = frozenset(
    {"bash", "powershell", "pwsh", "cmd", "wsl", "monitor"}
)
HOST_FILE_MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "edit",
        "write",
        "multiedit",
        "patch",
        "applypatch",
        "apply_patch",
        "notebookedit",
        "str_replace_based_edit_tool",
        "update",
    }
)
HOST_MUTATION_TOOLS: frozenset[str] = HOST_SHELL_TOOLS | HOST_FILE_MUTATION_TOOLS


def normalize_tool_name(name: object) -> str:
    return str(name or "").strip().lower()


# ── companions, loaded by FILE PATH ─────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name: str):
    """Load ``<name>.py`` (or its deployed ``aidocs_<name>.py`` copy) from this
    file's directory, without any package. Raises ImportError when absent."""
    key = "_aidocs_floor_" + name
    cached = sys.modules.get(key)
    if cached is not None:
        return cached
    for fname in ("aidocs_" + name + ".py", name + ".py"):
        path = os.path.join(_HERE, fname)
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(key, path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module  # dataclasses resolve their module here
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
        return module
    raise ImportError(f"host floor companion {name!r} is not deployed beside {_HERE}")


_peel = _load_sibling("shell_wrapper_peel")


# ── environment (install-level facts only) ──────────────────────────────────


def _environ(env: dict | None) -> dict:
    return dict(os.environ) if env is None else dict(env)


def _home(env: dict) -> str:
    keys = ("USERPROFILE", "HOME") if _WINDOWS else ("HOME",)
    for k in keys:
        if env.get(k):
            return str(env[k])
    return os.path.expanduser("~")


def _temp_dir(env: dict) -> str:
    keys = ("TEMP", "TMP", "TMPDIR") if _WINDOWS else ("TMPDIR", "TEMP", "TMP")
    for k in keys:
        if env.get(k):
            return str(env[k])
    if _WINDOWS and env.get("LOCALAPPDATA"):
        return os.path.join(str(env["LOCALAPPDATA"]), "Temp")
    return "/tmp"


# ── path law ────────────────────────────────────────────────────────────────


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _is_strict_descendant(child: str, root: str) -> bool:
    c, r = _norm(child), _norm(root)
    if c == r:
        return False
    try:
        return os.path.commonpath([c, r]) == r
    except ValueError:  # different drives / mixed absolute-relative
        return False


def _is_ancestor_or_equal(path: str, other: str) -> bool:
    """Is ``path`` the same as ``other`` or one of its ancestors?"""
    return _norm(path) == _norm(other) or _is_strict_descendant(other, path)


def _canonical(p: str) -> str:
    """Resolve links on the EXISTING prefix of ``p`` and re-append the rest."""
    cur = p
    tail: list[str] = []
    while not os.path.lexists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        tail.append(os.path.basename(cur))
        cur = parent
    real = os.path.realpath(cur)
    for part in reversed(tail):
        real = os.path.join(real, part)
    return os.path.normpath(real)


def _variants(p: str) -> list[str]:
    """Every reading of ``p`` that must be in scope: the lexical one and the
    link-resolved ones. A target is scoped only if ALL of them are."""
    out = [os.path.normpath(p)]
    for cand in (os.path.normpath(p), p):
        resolved = _canonical(cand)
        if resolved not in out:
            out.append(resolved)
    return out


class _Root:
    __slots__ = ("forms", "kind", "path")

    def __init__(self, path: str, kind: str) -> None:
        self.path = path
        self.kind = kind
        forms = [os.path.normpath(path)]
        try:
            real = _canonical(path)
            if real not in forms:
                forms.append(real)
        except OSError:
            pass
        self.forms = forms

    def admits(self, target: str) -> bool:
        """Strictly INSIDE a session scratchpad (the only root kind)."""
        for form in self.forms:
            if not _is_strict_descendant(target, form):
                continue
            rel = os.path.relpath(_norm(target), _norm(form))
            parts = [x for x in re.split(r"[\\/]+", rel) if x]
            # <project>/<session>/scratchpad/<child...> -- strictly INSIDE a
            # session scratchpad, never the scratchpad or anything above it.
            if len(parts) >= 4 and parts[2].lower() == "scratchpad":
                return True
        return False


def _scratch_roots(env: dict) -> list[_Root]:
    """THE scratch authority: the Claude Code session scratchpad, derived
    from TEMP alone. Deliberately no declaration file and no setting (r0b2
    architectural ruling) -- nothing an agent can write widens it."""
    return [_Root(os.path.join(_temp_dir(env), "claude"), "claude_session")]




# ── analysis context ────────────────────────────────────────────────────────


class Finding:
    __slots__ = ("primitive", "target", "why")

    def __init__(self, primitive: str, target: str, why: str) -> None:
        self.primitive = primitive
        self.target = target
        self.why = why

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.primitive!r}, {self.target!r}, {self.why!r})"


class _Ctx:
    def __init__(
        self,
        *,
        env: dict,
        cwds: list[str],
        dialect: str,
        roots: list[_Root],
        findings: list[Finding],
        depth: int = 0,
        wsl: bool = False,
        container: bool = False,
        stdin_args: bool = False,
        full_text: str = "",
        budget: list[int] | None = None,
        stdin_file: str | None = None,
    ) -> None:
        self.budget = budget if budget is not None else [0]
        self.stdin_file = stdin_file
        self.env = env
        self.cwds = cwds
        self.dialect = dialect
        self.roots = roots
        self.findings = findings
        self.depth = depth
        self.wsl = wsl
        self.container = container
        self.stdin_args = stdin_args
        self.full_text = full_text

    def child(self, **kw) -> _Ctx:
        base = {
            "env": self.env,
            "cwds": list(self.cwds),
            "dialect": self.dialect,
            "roots": self.roots,
            "findings": self.findings,
            "depth": self.depth,
            "wsl": self.wsl,
            "container": self.container,
            "stdin_args": False,
            "full_text": self.full_text,
            "budget": self.budget,
            "stdin_file": None,
        }
        base.update(kw)
        return _Ctx(**base)

    def add(self, primitive: str, target: str, why: str) -> None:
        self.findings.append(Finding(primitive, target, why))


_DYNAMIC_CHARS = re.compile(r"[$`*?\[\]{}]")
_CMD_VAR = re.compile(r"%[^%\s]+%|![^!\s]+!")
_PS_PROVIDER = re.compile(r"^[A-Za-z][A-Za-z0-9_]+:")
_NULL_SINKS = {"/dev/null", "nul", "$null", "/dev/stdout", "/dev/stderr", "con"}
_DEVICE_RE = re.compile(r"^/dev/(?:sd[a-z]|nvme\d|hd[a-z]|vd[a-z]|xvd[a-z]|mmcblk\d|disk\d|md\d|dm-)")


def _unbounded(raw: str, ctx: _Ctx) -> bool:
    if not raw or _DYNAMIC_CHARS.search(raw):
        return True
    if ctx.dialect == "cmd" and _CMD_VAR.search(raw):
        return True
    if "(" in raw and ctx.dialect == "ps":
        return True
    return False


def _to_abs_paths(raw: str, ctx: _Ctx) -> list[str] | None:
    """Absolute OS paths ``raw`` may denote (one per candidate cwd), or None
    when the target cannot be bounded."""
    if _unbounded(raw, ctx):
        return None
    s = raw
    if s.startswith("~"):
        if s == "~" or s[1] in "/\\":
            s = _home(ctx.env) + s[1:]
        else:
            return None  # ~user
    if ctx.wsl:
        if s.startswith("/"):
            m = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", s)
            if not m or not _WINDOWS:
                return None  # the Linux namespace is never host scratch
            s = m.group(1).upper() + ":\\" + (m.group(2) or "").replace("/", "\\")
    elif not _WINDOWS and ctx.dialect in ("cmd", "ps") and "\\" in s:
        s = s.replace("\\", "/")  # a Windows-dialect path read on a POSIX host
    elif _WINDOWS and ctx.dialect == "posix" and s.startswith("/"):
        m = re.match(r"^/(?:cygdrive/)?([A-Za-z])(?:/(.*))?$", s)
        if m:
            s = m.group(1).upper() + ":\\" + (m.group(2) or "").replace("/", "\\")
        elif s == "/tmp" or s.startswith("/tmp/"):
            s = _temp_dir(ctx.env) + s[4:]
    if _PS_PROVIDER.match(s) and not re.match(r"^[A-Za-z]:", s):
        return None  # HKLM:, Env:, Function: ... not a filesystem path
    if os.path.isabs(s) or re.match(r"^[A-Za-z]:[\\/]", s):
        return [s]
    if not ctx.cwds:
        return None
    return [os.path.join(c, s) for c in ctx.cwds]


def _scoped(raw: str, ctx: _Ctx, *, allow_cwd: bool = False) -> bool:
    """Is ``raw`` provably a strict descendant of the session scratchpad,
    and none of: the home directory, the cwd, or an ancestor of either?"""
    if ctx.container:
        return False
    paths = _to_abs_paths(raw, ctx)
    if not paths:
        return False
    home = _home(ctx.env)
    protected = [home, _canonical(home)]
    if not allow_cwd:
        for c in ctx.cwds:
            protected.extend([c, _canonical(c)])
    for p in paths:
        try:
            variants = _variants(p)
        except (OSError, ValueError):
            return False
        for v in variants:
            if not any(r.admits(v) for r in ctx.roots):
                return False
            if any(_is_ancestor_or_equal(v, f) for f in protected):
                return False
    return True


def _check_target(primitive: str, raw: str, ctx: _Ctx, *, allow_cwd: bool = False) -> None:
    if _unbounded(raw, ctx):
        ctx.add(primitive, raw, "the target is a glob / variable / substitution and cannot be bounded")
        return
    if not _scoped(raw, ctx, allow_cwd=allow_cwd):
        where = "inside a container" if ctx.container else "outside the Claude Code session scratchpad"
        ctx.add(primitive, raw, f"the target is {where} (or is a scratch root, home, the cwd or an ancestor)")


# ── tokenizers ──────────────────────────────────────────────────────────────


class _ParseError(Exception):
    pass


class _Segment:
    __slots__ = ("argv", "heredoc", "redirs", "sep")

    def __init__(self, sep: str) -> None:
        self.argv: list[str] = []
        self.redirs: list[tuple[str, str]] = []
        self.heredoc: str | None = None
        self.sep = sep


def _capture_balanced(text: str, i: int, open_ch: str, close_ch: str) -> int:
    """Index just past the ``close_ch`` matching the ``open_ch`` at ``i``.

    Quote-aware first. A heredoc inside ``$(...)`` (the everyday
    ``git commit -m "$(cat <<'EOF' ... EOF)"``) carries apostrophes that are
    NOT quotes, so on failure the match is retried counting brackets only.
    """
    try:
        return _capture_balanced_quoted(text, i, open_ch, close_ch)
    except _ParseError:
        depth = 0
        for j in range(i, len(text)):
            if text[j] == open_ch:
                depth += 1
            elif text[j] == close_ch:
                depth -= 1
                if depth == 0:
                    return j + 1
        raise


def _capture_balanced_quoted(text: str, i: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    n = len(text)
    quote: str | None = None
    j = i
    while j < n:
        ch = text[j]
        if quote:
            if ch == "\\" and quote == '"' and j + 1 < n:
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "\\" and j + 1 < n:
            j += 2
            continue
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise _ParseError(f"unbalanced {open_ch}{close_ch}")


_REDIR_OP = re.compile(r"(&>>|&>|>>|>\||>&|>|<<<|<<-|<<|<&|<>|<)")


def _parse_posix(text: str) -> tuple[list[_Segment], list[str]]:
    segs: list[_Segment] = [_Segment("")]
    subs: list[str] = []
    word: list[str] = []
    in_word = False
    pending_redir: str | None = None
    pending_heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word, pending_redir
        if not in_word:
            return
        w = "".join(word)
        word.clear()
        in_word = False
        if pending_redir is not None:
            op = pending_redir
            pending_redir = None
            if op in ("<<", "<<-"):
                pending_heredocs.append((w, op == "<<-"))
            segs[-1].redirs.append((op, w))
        else:
            segs[-1].argv.append(w)

    def new_segment(sep: str) -> None:
        flush()
        if pending_redir is not None:
            raise _ParseError("redirect without a target")
        segs.append(_Segment(sep))

    while i < n:
        c = text[i]
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            if i + 1 < n:
                word.append(text[i + 1])
                in_word = True
            i += 2
            continue
        if c == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise _ParseError("unterminated single quote")
            word.append(text[i + 1 : end])
            in_word = True
            i = end + 1
            continue
        if c == '"':
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise _ParseError("unterminated double quote")
                ch = text[j]
                if ch == "\\" and j + 1 < n and text[j + 1] in '$`"\\\n':
                    buf.append(text[j + 1] if text[j + 1] != "$" else "\\$")
                    j += 2
                    continue
                if ch == "$" and j + 1 < n and text[j + 1] == "(":
                    end = _capture_balanced(text, j + 1, "(", ")")
                    if not text.startswith("((", j + 1):
                        subs.append(text[j + 2 : end - 1])
                    buf.append(text[j:end])
                    j = end
                    continue
                if ch == "`":
                    end = text.find("`", j + 1)
                    if end < 0:
                        raise _ParseError("unterminated backtick")
                    subs.append(text[j + 1 : end])
                    buf.append(text[j : end + 1])
                    j = end + 1
                    continue
                if ch == '"':
                    break
                buf.append(ch)
                j += 1
            word.append("".join(buf))
            in_word = True
            i = j + 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "(":
            end = _capture_balanced(text, i + 1, "(", ")")
            if not text.startswith("((", i + 1):
                subs.append(text[i + 2 : end - 1])
            word.append(text[i:end])
            in_word = True
            i = end
            continue
        if c == "`":
            end = text.find("`", i + 1)
            if end < 0:
                raise _ParseError("unterminated backtick")
            subs.append(text[i + 1 : end])
            word.append(text[i : end + 1])
            in_word = True
            i = end + 1
            continue
        if c in "<>" and i + 1 < n and text[i + 1] == "(" and not in_word:
            end = _capture_balanced(text, i + 1, "(", ")")
            subs.append(text[i + 2 : end - 1])
            word.append(text[i:end])
            in_word = True
            i = end
            continue
        if c == "#" and not in_word:
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c in "<>" or (c == "&" and i + 1 < n and text[i + 1] == ">"):
            # a pure-digit word right before is an fd number, not an argument
            if in_word and "".join(word).isdigit():
                word.clear()
                in_word = False
            flush()
            m = _REDIR_OP.match(text, i)
            op = m.group(1) if m else c
            i += len(op)
            if op in (">&", "<&"):
                # fd duplication (2>&1): consume the fd word, no file target
                while i < n and text[i] in " \t":
                    i += 1
                while i < n and (text[i].isdigit() or text[i] == "-"):
                    i += 1
                continue
            pending_redir = op
            continue
        if c == "\n":
            flush()
            if pending_heredocs:
                bodies: list[str] = []
                rest = text[i + 1 :]
                lines = rest.split("\n")
                consumed = 0
                for delim, _strip_tabs in pending_heredocs:
                    body: list[str] = []
                    while consumed < len(lines):
                        line = lines[consumed]
                        consumed += 1
                        if line.strip() == delim:
                            break
                        body.append(line)
                    bodies.append("\n".join(body))
                pending_heredocs.clear()
                segs[-1].heredoc = "\n".join(bodies)
                skipped = sum(len(x) + 1 for x in lines[:consumed])
                i = i + 1 + skipped
                new_segment("\n")
                continue
            new_segment("\n")
            i += 1
            continue
        if c in ";&|()":
            two = text[i : i + 2]
            if two in ("&&", "||", "|&", ";;"):
                new_segment(two)
                i += 2
                continue
            new_segment(c)
            i += 1
            continue
        word.append(c)
        in_word = True
        i += 1
    flush()
    if pending_redir is not None:
        raise _ParseError("redirect without a target")
    if pending_heredocs:
        segs[-1].heredoc = ""
    return [s for s in segs if s.argv or s.redirs], subs


def _parse_ps(text: str) -> tuple[list[_Segment], list[str]]:
    segs: list[_Segment] = [_Segment("")]
    subs: list[str] = []
    word: list[str] = []
    in_word = False
    pending_redir: str | None = None
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word, pending_redir
        if not in_word:
            return
        w = "".join(word)
        word.clear()
        in_word = False
        if pending_redir is not None:
            segs[-1].redirs.append((pending_redir, w))
            pending_redir = None
        else:
            segs[-1].argv.append(w)

    def new_segment(sep: str) -> None:
        flush()
        segs.append(_Segment(sep))

    while i < n:
        c = text[i]
        if c == "`":
            if i + 1 < n:
                word.append(text[i + 1])
                in_word = True
            i += 2
            continue
        if c == "'":
            j = i + 1
            buf: list[str] = []
            while True:
                if j >= n:
                    raise _ParseError("unterminated single quote")
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            word.append("".join(buf))
            in_word = True
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while True:
                if j >= n:
                    raise _ParseError("unterminated double quote")
                ch = text[j]
                if ch == "`" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if ch == "$" and j + 1 < n and text[j + 1] == "(":
                    end = _capture_balanced(text, j + 1, "(", ")")
                    subs.append(text[j + 2 : end - 1])
                    buf.append(text[j:end])
                    j = end
                    continue
                if ch == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(ch)
                j += 1
            word.append("".join(buf))
            in_word = True
            i = j + 1
            continue
        if c in "$@" and i + 1 < n and text[i + 1] == "(":
            end = _capture_balanced(text, i + 1, "(", ")")
            subs.append(text[i + 2 : end - 1])
            word.append(text[i:end])
            in_word = True
            i = end
            continue
        if c == "(":
            end = _capture_balanced(text, i, "(", ")")
            subs.append(text[i + 1 : end - 1])
            word.append(text[i:end])
            in_word = True
            i = end
            continue
        if c == "<" and i + 1 < n and text[i + 1] == "#":
            end = text.find("#>", i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == "#" and not in_word:
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == ">" or (c in "*0123456789" and i + 1 < n and text[i + 1] == ">" and not in_word):
            flush()
            m = re.compile(r"[*\d]?>>?(&\d)?").match(text, i)
            op = m.group(0) if m else ">"
            i += len(op)
            if "&" in op:
                continue
            pending_redir = ">>" if ">>" in op else ">"
            continue
        if c in ";|\n{}":
            two = text[i : i + 2]
            if two in ("&&", "||"):
                new_segment(two)
                i += 2
                continue
            new_segment(c)
            i += 1
            continue
        if c == "&" and i + 1 < n and text[i + 1] == "&":
            new_segment("&&")
            i += 2
            continue
        word.append(c)
        in_word = True
        i += 1
    flush()
    return [s for s in segs if s.argv or s.redirs], subs


def _parse_cmd(text: str) -> tuple[list[_Segment], list[str]]:
    segs: list[_Segment] = [_Segment("")]
    word: list[str] = []
    in_word = False
    pending_redir: str | None = None
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal in_word, pending_redir
        if not in_word:
            return
        w = "".join(word)
        word.clear()
        in_word = False
        if pending_redir is not None:
            segs[-1].redirs.append((pending_redir, w))
            pending_redir = None
        else:
            segs[-1].argv.append(w)

    while i < n:
        c = text[i]
        if c == "^" and i + 1 < n:
            word.append(text[i + 1])
            in_word = True
            i += 2
            continue
        if c == '"':
            end = text.find('"', i + 1)
            if end < 0:
                end = n  # cmd tolerates a missing closing quote
            word.append(text[i + 1 : end])
            in_word = True
            i = end + 1
            continue
        if c in " \t\r,;=" and not (c in ",;=" and in_word):
            flush()
            i += 1
            continue
        if c == ">" or (c in "12" and i + 1 < n and text[i + 1] == ">" and not in_word):
            flush()
            m = re.compile(r"[12]?>>?(&\d)?").match(text, i)
            op = m.group(0) if m else ">"
            i += len(op)
            if "&" in op:
                continue
            pending_redir = ">>" if ">>" in op else ">"
            continue
        if c in "&|\n()":
            flush()
            two = text[i : i + 2]
            sep = two if two in ("&&", "||") else c
            segs.append(_Segment(sep))
            i += len(sep)
            continue
        word.append(c)
        in_word = True
        i += 1
    flush()
    return [s for s in segs if s.argv or s.redirs], []


_PARSERS = {"posix": _parse_posix, "ps": _parse_ps, "cmd": _parse_cmd}


# ── the destructive-keyword sniff (opaque payloads only) ────────────────────
_SNIFF = re.compile(
    r"(?i)(\brm\s+(-\w*[rR]|--recursive)|rmtree|rmSync|rmdirSync|remove-item|\bri\s|"
    r"\b(rd|rmdir)\s+/s|\bdel\s+/[sq]|format-volume|clear-disk|diskpart|clear-content|"
    r"reset\s+--hard|\bclean\s+-\w*f|push\s[^|;&]*(--force|\s-f\b|\s\+)|"
    r"Directory\]?::Delete|--remove-files|/mir\b|/purge\b|-delete\b|\bshred\b|"
    r"\btruncate\b|\bmkfs|os\.(remove|unlink|system)|unlinkSync|FileUtils\.rm)"
)


# ── analysis ────────────────────────────────────────────────────────────────

_POSIX_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}",
     "time", "case", "esac", "function", "select", "coproc", "in"}
)
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "fish", "busybox"})
_PWSH = frozenset({"pwsh", "powershell", "powershell_ise"})
_PY_RE = re.compile(r"^(python(\d+(\.\d+)?)?w?|py|pypy3?)$")
_NODE = frozenset({"node", "nodejs", "bun", "deno"})
_CD = frozenset({"cd", "pushd", "chdir", "set-location", "sl", "push-location"})
_PS_REMOVE = frozenset({"remove-item", "ri", "rm", "del", "erase", "rd", "rmdir"})
_DISK_KILLERS = frozenset(
    {"format", "format-volume", "clear-disk", "initialize-disk", "remove-partition",
     "remove-volume", "diskpart", "wipefs", "fdisk", "sfdisk", "parted", "mkswap"}
)
_DOCKERS = frozenset({"docker", "podman", "nerdctl"})
#: Carriers whose command-string option this module parses itself; the shared
#: peeler's whitespace reading of the same option is redundant for them (and
#: following both doubles the work at every nesting level).
_EXPLICIT_CARRIERS = _SHELLS | frozenset({"su"})


def _base(token: str) -> str:
    b = _peel.basename_token(token).lower()
    for ext in (".exe", ".cmd", ".bat", ".com"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


def analyze_script(text: str, ctx: _Ctx) -> None:
    ctx.budget[0] += 1
    if ctx.budget[0] > MAX_ANALYSES:
        if ctx.budget[0] == MAX_ANALYSES + 1:
            ctx.add("carrier", text[:80], "the command is too complex to analyse within the floor's budget")
        return
    if ctx.depth > MAX_DEPTH:
        ctx.add("carrier", text[:80], f"wrapper/carrier nesting exceeds the analysis cap ({MAX_DEPTH})")
        return
    try:
        segments, subs = _PARSERS[ctx.dialect](text)
    except _ParseError as exc:
        if ctx.depth == 0:
            if _SNIFF.search(text):
                ctx.add("unparseable", text[:80], f"a destructive shape in a command that cannot be parsed ({exc})")
        else:
            ctx.add("carrier", text[:80], f"a carrier payload that cannot be parsed ({exc})")
        return
    for sub in subs:
        analyze_script(sub, ctx.child(depth=ctx.depth + 1))
    cwds = list(ctx.cwds)
    for seg in segments:
        seg_ctx = ctx.child(cwds=cwds)
        argv = _strip_leading(seg.argv, ctx.dialect)
        if argv and _base(argv[0]) in _CD:
            cwds = _after_cd(argv, seg_ctx)
            continue
        _analyze_segment(seg, argv, seg_ctx)
    if ctx.dialect == "ps":
        _scan_dotnet(text, ctx)


def _strip_leading(argv: list[str], dialect: str) -> list[str]:
    out = list(argv)
    if dialect == "posix":
        while out and (out[0] in _POSIX_KEYWORDS or _peel.ENV_ASSIGN_RE.match(out[0])):
            out = out[1:]
        while out and out[-1] in ("}", "fi", "done", "esac"):
            out = out[:-1]
    elif dialect == "ps":
        while out and out[0] in ("&", ".", "{", "}"):
            out = out[1:]
    elif dialect == "cmd":
        while out and out[0].lower() in ("@echo", "call", "@") and len(out) > 1 and out[0].lower() != "@echo":
            out = out[1:]
    return out


def _after_cd(argv: list[str], ctx: _Ctx) -> list[str]:
    args = [a for a in argv[1:] if not a.startswith("-") or a == "-"]
    if not args:
        target = "~"
    else:
        target = args[0]
    paths = _to_abs_paths(target, ctx) if target != "-" else None
    if paths is None:
        return []  # unknown cwd: every later relative target is unbounded
    # The cd may fail (`cd x || true`), so the OLD cwds stay candidates too.
    out = list(ctx.cwds)
    for p in paths:
        if p not in out:
            out.append(p)
    return out


_TRUNCATING_FORMS = ([":"], ["true"], ["cat", "/dev/null"], ["echo", "-n"], ["type", "nul"])


def _analyze_segment(seg: _Segment, argv: list[str], ctx: _Ctx) -> None:
    for op, target in seg.redirs:
        if op == "<" and argv:
            ctx = ctx.child(stdin_file=target, stdin_args=ctx.stdin_args)
        if op in ("<", "<<<", "<<", "<<-"):
            continue
        # EVERY output redirect (>, >>, >|, 2>, &>, <>) is a write: host-control
        # territory is checked first, before any scratch allowance.
        if target.lower() not in _NULL_SINKS and _check_control_write("redirect " + op, target, ctx):
            continue
        if op == "<>":
            continue
        if _DEVICE_RE.match(target):
            ctx.add("device_write", target, "a raw write to a block device")
            continue
        if target.lower() in _NULL_SINKS:
            continue
        if op in (">", ">|", "&>") and (not argv or [a.lower() for a in argv] in _TRUNCATING_FORMS):
            _check_target("truncate_redirect", target, ctx)
    if seg.heredoc is not None and argv:
        _analyze_heredoc(argv, seg.heredoc, ctx)
    if not argv:
        return
    _analyze_argv(argv, ctx, piped=seg.sep in ("|", "|&"))


def _analyze_heredoc(argv: list[str], body: str, ctx: _Ctx) -> None:
    for cand in _peel.peel_wrapper_candidates(list(argv)):
        if not cand:
            continue
        b = _base(cand[0])
        if b in _SHELLS and not any(t.startswith("-") and "c" in t[1:] for t in cand[1:]):
            analyze_script(body, ctx.child(dialect="posix", depth=ctx.depth + 1))
        elif b in _PWSH:
            analyze_script(body, ctx.child(dialect="ps", depth=ctx.depth + 1))
        elif _PY_RE.match(b) and not any(t == "-c" for t in cand[1:]):
            _scan_python(body, ctx)
        elif b in _NODE and not any(t in ("-e", "--eval", "-p", "--print") for t in cand[1:]):
            _scan_node(body, ctx)


def _analyze_argv(argv: list[str], ctx: _Ctx, *, piped: bool = False) -> None:
    if ctx.depth > MAX_DEPTH:
        ctx.add("carrier", " ".join(argv)[:80], f"wrapper nesting exceeds the analysis cap ({MAX_DEPTH})")
        return
    candidates = _peel.peel_wrapper_candidates(list(argv))
    original = list(argv)
    explicit = any(c and _base(c[0]) in _EXPLICIT_CARRIERS for c in candidates)
    for cand in candidates:
        if not cand:
            continue
        is_original = cand == original
        consumed = argv[: max(0, len(argv) - len(cand))]
        via_xargs = any(_base(t) == "xargs" for t in consumed)
        sub_ctx = ctx.child(stdin_args=via_xargs or ctx.stdin_args, stdin_file=ctx.stdin_file)
        if not is_original and _base(cand[0]) in _peel.WRAPPER_TABLES:
            # the shared peeler stopped at its own cap: keep peeling, counted
            _analyze_argv(cand, sub_ctx.child(depth=ctx.depth + 1, stdin_args=sub_ctx.stdin_args,
                                              stdin_file=ctx.stdin_file))
            continue
        if not is_original and " " in cand[0].strip() and not explicit:
            # a command STRING carried as one token (`su -c`, `eval`, `sh -c`)
            analyze_script(" ".join(cand), sub_ctx.child(dialect="posix", depth=ctx.depth + 1))
            continue
        _classify(cand, sub_ctx, piped=piped and is_original)


def _classify(argv: list[str], ctx: _Ctx, *, piped: bool) -> None:  # noqa: C901 - one dispatch table
    b = _base(argv[0])
    args = argv[1:]
    d = ctx.dialect
    if not (
        b in _READ_ONLY_BASES
        or b in _peel.WRAPPER_TABLES
        # Interpreters / carriers: their operands are CODE or a script path;
        # the code is scanned and the script read, write-checks and all.
        or b in _SHELLS or b in _PWSH or b in _NODE or _PY_RE.match(b)
        or b in ("cmd", "wsl", "su", "perl", "ruby")
    ):
        _control_operands(b, args, ctx)
    if _direct_script(argv[0], ctx):
        return
    if b in _SHELLS:
        _shell_carrier(b, args, ctx, piped=piped)
    elif b == "cmd":
        _cmd_carrier(args, ctx)
    elif b in _PWSH:
        _pwsh_carrier(args, ctx, piped=piped)
    elif b == "wsl":
        _wsl_carrier(args, ctx)
    elif b == "su":
        for i, t in enumerate(args):
            if t in ("-c", "--command") and i + 1 < len(args):
                analyze_script(args[i + 1], ctx.child(dialect="posix", depth=ctx.depth + 1))
    elif b in ("start-process", "saps", "start"):
        _start_process(args, ctx)
    elif b in _DOCKERS or b == "docker-compose":
        _docker(b, args, ctx)
    elif b == "git":
        _git(args, ctx)
    elif b == "find":
        _find(args, ctx)
    elif _PY_RE.match(b):
        _python(args, ctx, piped=piped)
    elif b in _NODE:
        _node(b, args, ctx, piped=piped)
    elif b in ("perl", "ruby"):
        for i, t in enumerate(args):
            if t in ("-e", "-E") and i + 1 < len(args) and _SNIFF.search(args[i + 1]):
                ctx.add(b + "_inline", args[i + 1][:80], "an inline delete whose target cannot be bounded")
    elif b in _DISK_KILLERS or b.startswith("mkfs"):
        if args and all(a.lower() in ("/?", "--help", "-h", "-?") for a in args):
            return
        ctx.add(b, " ".join(args)[:80], "a volume/disk destroyer has no scratch scope")
    # DIALECT-INDEPENDENT recognition of the Windows delete family: the
    # primitive is recognised by its own shape in any payload, not only when
    # the payload happens to be parsed as cmd / PowerShell.
    elif b in ("remove-item", "ri") or (d == "ps" and b in _PS_REMOVE):
        _ps_remove(args, ctx, piped=piped)
    elif b in ("del", "erase"):
        _cmd_del(b, args, ctx)
    elif b in ("rd", "rmdir") and (d == "cmd" or any(a.lower() == "/s" for a in args)):
        _cmd_rd(b, args, ctx)
    elif b == "rm":
        _gnu_rm(args, ctx)
    elif b in ("clear-content", "clc"):
        _targets_of_ps(b, args, ctx, piped=piped)
    elif b == "truncate":
        _plain_targets(b, args, ctx, value_opts={"-s", "--size", "-r", "--reference"})
    elif b == "shred":
        _plain_targets(b, args, ctx, value_opts={"-n", "--iterations", "-s", "--size", "--random-source"})
    elif b == "dd":
        for a in args:
            if a.lower().startswith("of="):
                tgt = a[3:]
                if _DEVICE_RE.match(tgt):
                    ctx.add("dd", tgt, "a raw write to a block device")
                elif tgt.lower() not in _NULL_SINKS:
                    _check_target("dd", tgt, ctx)
    elif b in ("cp", "mv", "copy", "move", "copy-item", "move-item", "cpi", "mi", "cpp"):
        _copy_move(b, args, ctx)
    elif b == "tar" or b == "bsdtar":
        _tar(args, ctx)
    elif b == "robocopy":
        if any(a.upper().startswith(("/MIR", "/PURGE")) for a in args):
            ops = [a for a in args if not a.startswith("/")]
            if len(ops) >= 2:
                _check_target("robocopy_mirror", ops[1], ctx)
            else:
                ctx.add("robocopy_mirror", " ".join(args), "a mirror without a bounded destination")
    elif b == "xargs":
        return  # peeled; a bare xargs runs echo
    elif (ctx.container or ctx.stdin_args) and b in ("rmdir", "unlink"):
        for t in args or ["<stdin>"]:
            _check_target(b, t, ctx)


# ── primitives ──────────────────────────────────────────────────────────────


def _gnu_rm(args: list[str], ctx: _Ctx) -> None:
    recursive = False
    targets: list[str] = []
    opts_done = False
    for a in args:
        if opts_done or not a.startswith("-") or a == "-":
            targets.append(a)
            continue
        if a == "--":
            opts_done = True
            continue
        if a.startswith("--"):
            if a == "--recursive":
                recursive = True
            continue
        if "r" in a[1:] or "R" in a[1:]:
            recursive = True
    if ctx.stdin_args:
        # xargs / find -exec: the operand stands for EVERY item fed in, which
        # is a tree-wide delete in all but name.
        recursive = True
    unbounded = [t for t in targets if _unbounded(t, ctx)]
    if not targets:
        if ctx.stdin_args:
            ctx.add("rm", "<stdin>", "delete targets arrive on stdin (xargs) and cannot be bounded")
        return
    if not recursive and not unbounded and not ctx.container:
        return  # single literal file(s): left to governance, not a scope floor
    for t in targets:
        _check_target("rm -r" if recursive else "rm", t, ctx)


def _ps_named_values(args: list[str], names: tuple[str, ...]) -> tuple[list[str], list[str], set[str]]:
    """(named values, positionals, switches) for a PowerShell cmdlet call."""
    values: list[str] = []
    positional: list[str] = []
    switches: set[str] = set()
    value_params = ("-filter", "-include", "-exclude", "-credential", "-stream", "-destination",
                    "-encoding", "-value")
    i = 0
    while i < len(args):
        a = args[i]
        al = a.lower()
        if al.startswith("-") and len(al) > 1 and not re.match(r"^-\d", al):
            name = al.split(":", 1)[0]
            if any(full.startswith(name) and len(name) >= 2 for full in names) or name in ("-lp",):
                if ":" in al:
                    values.append(a.split(":", 1)[1])
                elif i + 1 < len(args):
                    values.append(args[i + 1])
                    i += 1
            elif any(vp.startswith(name) and len(name) >= 3 for vp in value_params):
                i += 1  # skip the value
            else:
                switches.add(name)
            i += 1
            continue
        positional.append(a)
        i += 1
    split: list[str] = []
    for v in values + positional:
        split.extend(x for x in v.split(",") if x != "")
    return split, positional, switches


def _ps_remove(args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    targets, _pos, switches = _ps_named_values(args, ("-path", "-literalpath", "-pspath"))
    if any("-whatif".startswith(s) and len(s) >= 3 for s in switches):
        return
    recursive = any(s.startswith("-r") for s in switches)
    if not targets:
        if piped or ctx.stdin_args:
            ctx.add("Remove-Item", "<pipeline>", "items arrive on the pipeline and cannot be bounded")
        return
    unbounded = [t for t in targets if _unbounded(t, ctx) or _PS_PROVIDER.match(t) and not re.match(r"^[A-Za-z]:", t)]
    if not recursive and not unbounded:
        return
    for t in targets:
        _check_target("Remove-Item -Recurse" if recursive else "Remove-Item", t, ctx)


def _targets_of_ps(prim: str, args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    targets, _pos, _sw = _ps_named_values(args, ("-path", "-literalpath", "-pspath"))
    if not targets:
        if piped:
            ctx.add(prim, "<pipeline>", "items arrive on the pipeline and cannot be bounded")
        return
    for t in targets:
        _check_target(prim, t, ctx)


def _cmd_del(prim: str, args: list[str], ctx: _Ctx) -> None:
    flags = {a.lower() for a in args if a.startswith("/")}
    targets = [a for a in args if not a.startswith("/")]
    recursive = "/s" in flags
    if not targets:
        return
    if not recursive and not any(_unbounded(t, ctx) for t in targets):
        return
    for t in targets:
        _check_target(prim + (" /s" if recursive else ""), t, ctx)


def _cmd_rd(prim: str, args: list[str], ctx: _Ctx) -> None:
    flags = {a.lower() for a in args if a.startswith("/")}
    if "/s" not in flags:
        return
    for t in (a for a in args if not a.startswith("/")):
        _check_target(prim + " /s", t, ctx)


def _plain_targets(prim: str, args: list[str], ctx: _Ctx, *, value_opts: set[str]) -> None:
    i = 0
    targets: list[str] = []
    while i < len(args):
        a = args[i]
        if a in value_opts:
            i += 2
            continue
        if a.startswith("-") and a != "-":
            i += 1
            continue
        targets.append(a)
        i += 1
    for t in targets:
        _check_target(prim, t, ctx)


def _existing_dir(raw: str, ctx: _Ctx) -> str | None:
    paths = _to_abs_paths(raw, ctx)
    if not paths:
        return None
    for p in paths:
        if os.path.isdir(p):
            return p
    return None


def _copy_move(b: str, args: list[str], ctx: _Ctx) -> None:
    is_move = b in ("mv", "move", "move-item", "mi")
    ps = ctx.dialect == "ps" or b in ("copy-item", "move-item", "cpi", "mi")
    dest: str | None = None
    no_target_dir = False
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        al = a.lower()
        if ps and al.startswith("-") and "-destination".startswith(al.split(":")[0]) and len(al) >= 3:
            dest = a.split(":", 1)[1] if ":" in al else (args[i + 1] if i + 1 < len(args) else None)
            i += 1 if ":" in al else 2
            continue
        if a in ("-T", "--no-target-directory"):
            no_target_dir = True
            i += 1
            continue
        if a in ("-t", "--target-directory") and i + 1 < len(args):
            dest = args[i + 1]
            i += 2
            continue
        if al.startswith("--target-directory="):
            dest = a.split("=", 1)[1]
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and not (ctx.dialect == "cmd"):
            if not a.startswith("--") and "T" in a[1:]:
                no_target_dir = True
            i += 1
            continue
        if a.startswith("/") and ctx.dialect == "cmd":
            i += 1
            continue
        operands.append(a)
        i += 1
    if dest is None:
        if len(operands) < 2:
            return
        dest, operands = operands[-1], operands[:-1]
    if dest.lower() in _NULL_SINKS:
        if is_move:
            for src in operands:
                _check_target(b + " into a null sink", src, ctx)
        return
    if _unbounded(dest, ctx):
        return
    dest_dir = _existing_dir(dest, ctx)
    if dest_dir is None:
        return
    if no_target_dir:
        _check_target(b + " over a directory", dest, ctx)
        return
    for src in operands:
        if _unbounded(src, ctx):
            continue
        child = os.path.join(dest_dir, os.path.basename(src.rstrip("/\\")))
        if os.path.isdir(child):
            _check_target(b + " over a directory", child, ctx)


def _tar(args: list[str], ctx: _Ctx) -> None:
    if not any(a == "--remove-files" for a in args):
        return
    operands: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file", "-C", "--directory", "-T", "--files-from", "-X", "--exclude-from"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if a.startswith("-") or (i == 0 and re.match(r"^[a-zA-Z]+$", a) and "f" in a):
            i += 2 if a.endswith("f") else 1
            continue
        operands.append(a)
        i += 1
    if not operands:
        ctx.add("tar --remove-files", "<unknown>", "the removed members cannot be bounded")
    for t in operands:
        _check_target("tar --remove-files", t, ctx)


def _find(args: list[str], ctx: _Ctx) -> None:
    i = 0
    while i < len(args) and args[i] in ("-H", "-L", "-P"):
        i += 1
    starts: list[str] = []
    while i < len(args) and not (args[i].startswith("-") or args[i] in ("(", "!", ")", "\\(")):
        starts.append(args[i])
        i += 1
    if not starts:
        starts = ["."]
    expr = args[i:]
    if "-delete" in expr:
        for s in starts:
            _check_target("find -delete", s, ctx, allow_cwd=True)
    j = 0
    while j < len(expr):
        if expr[j] in ("-exec", "-execdir", "-ok", "-okdir"):
            k = j + 1
            payload: list[str] = []
            while k < len(expr) and expr[k] not in (";", "+", "\\;"):
                payload.append(expr[k])
                k += 1
            if payload:
                for s in starts:
                    substituted = [s if t == "{}" else t for t in payload]
                    _analyze_argv(substituted, ctx.child(depth=ctx.depth + 1, stdin_args=True))
            j = k + 1
            continue
        j += 1


# ── git ─────────────────────────────────────────────────────────────────────


def _cluster(args: list[str], letter: str) -> bool:
    return any(a.startswith("-") and not a.startswith("--") and letter in a[1:] for a in args)


def _git_checkout_discards(rest: list[str], path_exists) -> bool:
    """Does this `git checkout` overwrite worktree files (r0b2 blocker 3)?

    Discarding: `-f`/`--force`; any pathspec after `--`; `--pathspec-from-
    file`; a tree-ish PLUS paths (`checkout HEAD .`, `checkout main a.py`);
    a single operand that is `.` / a glob / `:/` or names an existing path.
    Not discarding: `checkout <branch>`, `-b/-B/--orphan` creation, `-p`."""
    rest_l = [r.lower() for r in rest]
    if "-p" in rest_l or "--patch" in rest_l:
        return False
    if "-f" in rest_l or "--force" in rest_l or any(r.startswith("--pathspec-from-file") for r in rest_l):
        return True
    if "--" in rest:
        return bool(rest[rest.index("--") + 1 :])
    ops: list[str] = []
    creating = False
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("-b", "-B", "--orphan"):
            creating = True
            i += 2
            continue
        if t in ("--conflict",):
            i += 2
            continue
        if t.startswith("-"):
            if t.startswith("--orphan="):
                creating = True
            i += 1
            continue
        ops.append(t)
        i += 1
    if creating:
        return False
    if len(ops) >= 2:
        return True
    if len(ops) == 1:
        o = ops[0]
        if o in (".", "*") or o.startswith(":/") or _DYNAMIC_CHARS.search(o):
            return True
        if path_exists is not None and path_exists(o):
            return True
    return False


def _git_what(sub: str, rest: list[str], path_exists=None) -> tuple[str | None, bool]:
    """(what, remote) for a git SUBCOMMAND + its arguments, any flag order."""
    rest_l = [r.lower() for r in rest]
    what: str | None = None
    remote = False
    if sub == "reset" and "--hard" in rest_l:
        what = "git reset --hard"
    elif sub == "checkout" and _git_checkout_discards(rest, path_exists):
        what = "git checkout (worktree overwrite)"
    elif sub == "restore":
        staged = "--staged" in rest_l or _cluster(rest, "S")
        worktree = "--worktree" in rest_l or _cluster(rest, "W")
        if worktree or not staged:  # the default target IS the worktree
            what = "git restore (worktree)"
    elif sub == "switch" and any(r in ("--discard-changes", "-f", "--force") for r in rest_l):
        what = "git switch --discard-changes"
    elif sub == "clean":
        forced = "--force" in rest_l or _cluster(rest, "f")
        dry = "--dry-run" in rest_l or _cluster(rest, "n")
        if forced and not dry:
            what = "git clean -f"
    elif sub == "branch":
        has_upper_d = _cluster(rest, "D")
        has_d = "--delete" in rest_l or _cluster(rest, "d")
        has_f = "--force" in rest_l or _cluster(rest, "f")
        if has_upper_d or (has_d and has_f):
            what = "git branch -D"
    elif sub == "stash" and rest_l[:1] and rest_l[0] in ("drop", "clear"):
        what = "git stash " + rest_l[0]
    elif sub == "update-ref" and "-d" in rest_l:
        what = "git update-ref -d"
    elif sub == "gc" and any(r.startswith("--prune=") and r.split("=", 1)[1] in ("now", "all") for r in rest_l):
        what = "git gc --prune=now"
    elif sub == "reflog" and rest_l[:1]:
        if rest_l[0] == "delete":
            what = "git reflog delete"
        elif rest_l[0] == "expire" and any(
            r.startswith(("--expire=", "--expire-unreachable=")) and r.split("=", 1)[1] in ("now", "all")
            for r in rest_l
        ):
            what = "git reflog expire --expire=now"
    elif sub == "push":
        if (
            any(r in ("--force", "--mirror", "--delete", "-d", "--prune") for r in rest_l)
            or any(r.startswith(("--force-with-lease", "--force-if-includes", "--force=")) for r in rest_l)
            or _cluster(rest, "f")
            or _cluster(rest, "d")
            or any((r.startswith("+") or r.startswith(":")) and len(r) > 1 for r in rest)
        ):
            what = "git push --force/--delete"
            remote = True
    return what, remote


_GIT_GLOBAL_VALUE_OPTS = ("-C", "-c", "--namespace", "--super-prefix", "--config-env",
                          "--exec-path", "--git-dir", "--work-tree")


def git_destructive_shape(argv: list[str]) -> tuple[str, str] | None:
    """``(what, subcommand)`` when ``argv`` (``git ...``) is a destroyer.

    Global options (``-C``, ``-c``, ``--git-dir``, ``--work-tree`` ...) are
    parsed BEFORE the subcommand, and the subcommand's flags are read in any
    order and any cluster -- the two ways the git deny table was defeated.
    Scope is NOT considered here; this is the SHAPE, shared with bash_policy.
    """
    if not argv or _base(argv[0]) != "git":
        return None
    args = argv[1:]
    i = 0
    while i < len(args):
        t = args[i]
        if t in _GIT_GLOBAL_VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return None
    sub = args[i].lower()
    what, _remote = _git_what(sub, args[i + 1 :])
    return (what, sub) if what else None


def _git(args: list[str], ctx: _Ctx) -> None:
    i = 0
    repo_cwds = list(ctx.cwds)
    work_tree: str | None = None
    git_dir: str | None = None
    unbounded_dir = False
    while i < len(args):
        t = args[i]
        if t == "-C" and i + 1 < len(args):
            val = args[i + 1]
            sub = ctx.child(cwds=repo_cwds)
            paths = _to_abs_paths(val, sub)
            if paths is None:
                unbounded_dir = True
                repo_cwds = []
            else:
                repo_cwds = paths
            i += 2
            continue
        if t in ("-c", "--namespace", "--super-prefix", "--config-env", "--exec-path") and i + 1 < len(args) and "=" not in t:
            i += 2
            continue
        if t == "--git-dir" and i + 1 < len(args):
            git_dir = args[i + 1]
            i += 2
            continue
        if t.startswith("--git-dir="):
            git_dir = t.split("=", 1)[1]
            i += 1
            continue
        if t == "--work-tree" and i + 1 < len(args):
            work_tree = args[i + 1]
            i += 2
            continue
        if t.startswith("--work-tree="):
            work_tree = t.split("=", 1)[1]
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(args):
        return
    probe_dirs = [work_tree] if work_tree is not None else list(repo_cwds)

    def _exists_in_repo(rel: str) -> bool:
        for base in probe_dirs:
            paths = _to_abs_paths(rel, ctx.child(cwds=[base] if os.path.isabs(base) else repo_cwds))
            if paths and any(os.path.exists(p) for p in paths):
                return True
        return False

    what, remote = _git_what(args[i].lower(), args[i + 1 :], _exists_in_repo)
    if what is None:
        return
    if remote:
        ctx.add(what, "<remote>", "rewrites or deletes remote history; no local scratch scope applies")
        return
    if unbounded_dir:
        ctx.add(what, "<git -C>", "the repository directory cannot be bounded")
        return
    dirs: list[str] = []
    rc = ctx.child(cwds=repo_cwds)
    if work_tree is not None:
        dirs.append(work_tree)
    else:
        if git_dir is not None:
            gd = git_dir.rstrip("/\\")
            dirs.append(os.path.dirname(gd) if os.path.basename(gd).lower() == ".git" else gd)
        dirs.extend(repo_cwds or [""])
    for dpath in dirs:
        if not dpath:
            ctx.add(what, "<cwd>", "the repository directory is unknown")
            continue
        _check_target(what, dpath, rc, allow_cwd=True)


# ── script-file carriers (r0b2 blocker 2) ──────────────────────────────────

#: Largest script the floor will read. A bigger one cannot be inspected
#: within the hook's budget and is refused (fail closed).
SCRIPT_MAX_BYTES = 256 * 1024
_SCRIPT_EXT = {
    ".py": "python", ".pyw": "python",
    ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node",
    ".sh": "posix", ".bash": "posix", ".zsh": "posix", ".ksh": "posix",
    ".ps1": "ps", ".psm1": "ps",
    ".bat": "cmd", ".cmd": "cmd",
}


def _decode_script(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data:
        return None  # binary
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _inspect_script(raw: str, kind: str, ctx: _Ctx, prim: str) -> None:
    """Read a local script a known interpreter will run and analyse it with
    the same shape logic. Anything that prevents inspection is a denial."""
    if _unbounded(raw, ctx):
        ctx.add(prim, raw, "the script path is a variable / glob / substitution and cannot be inspected")
        return
    paths = _to_abs_paths(raw, ctx)
    if not paths:
        ctx.add(prim, raw, "the script path cannot be resolved and cannot be inspected")
        return
    for p in paths:
        if not os.path.isfile(p) and kind == "node" and os.path.isfile(p + ".js"):
            p = p + ".js"
        if not os.path.isfile(p):
            ctx.add(prim, raw, "the script does not exist (or is not a file) and cannot be inspected")
            continue
        try:
            size = os.path.getsize(p)
            if size > SCRIPT_MAX_BYTES:
                ctx.add(prim, raw, f"the script is over the {SCRIPT_MAX_BYTES // 1024} KiB inspection cap")
                continue
            with open(p, "rb") as fh:
                data = fh.read(SCRIPT_MAX_BYTES + 1)
        except OSError as exc:
            ctx.add(prim, raw, f"the script cannot be read ({type(exc).__name__})")
            continue
        text = _decode_script(data)
        if text is None:
            ctx.add(prim, raw, "the script is binary / undecodable and cannot be inspected")
            continue
        sub = ctx.child(depth=ctx.depth + 1, full_text=text)
        if sub.depth > MAX_DEPTH:
            ctx.add(prim, raw, f"script nesting exceeds the analysis cap ({MAX_DEPTH})")
            continue
        if kind == "python":
            _scan_python(text, sub)
        elif kind == "node":
            _scan_node(text, sub)
        else:
            analyze_script(text, sub.child(dialect=kind))


def _direct_script(token: str, ctx: _Ctx) -> bool:
    """`./x.sh`, `.\\x.ps1`, `scripts\\x.bat`, `C:/x/y.py`: a script file run
    directly. A PATH-like token is always inspected; a bare name only when it
    exists in the cwd (otherwise it is a PATH program such as `npm.cmd`)."""
    raw = token.strip("\"'")
    ext = os.path.splitext(raw.lower())[1]
    kind = _SCRIPT_EXT.get(ext)
    if kind is None:
        return False
    if any(ch in raw for ch in "/\\") or raw.startswith("."):
        _inspect_script(raw, kind, ctx, "script " + ext)
        return True
    paths = None if _unbounded(raw, ctx) else _to_abs_paths(raw, ctx)
    if paths and any(os.path.isfile(p) for p in paths):
        _inspect_script(raw, kind, ctx, "script " + ext)
        return True
    return False


# ── carriers ────────────────────────────────────────────────────────────────


def _shell_carrier(b: str, args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    if b == "busybox" and args and _base(args[0]) in _SHELLS:
        _shell_carrier(_base(args[0]), args[1:], ctx, piped=piped)
        return
    skip = False
    for i, t in enumerate(args):
        if skip:
            skip = False
            continue
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            if i + 1 < len(args):
                analyze_script(args[i + 1], ctx.child(dialect="posix", depth=ctx.depth + 1))
            return
        if t in ("-o", "+o"):
            skip = True
            continue
        if not t.startswith(("-", "+")):
            _inspect_script(t, "posix", ctx, b + " script")  # r0b2 blocker 2
            return
    if ctx.stdin_file:
        _inspect_script(ctx.stdin_file, "posix", ctx, b + " < script")
        return
    if piped and _SNIFF.search(ctx.full_text):
        ctx.add(b, "<stdin>", "a shell fed a destructive script on stdin")


def _cmd_carrier(args: list[str], ctx: _Ctx) -> None:
    for i, t in enumerate(args):
        tl = t.lower()
        if tl in ("/c", "/k", "/r") or re.match(r"^/[a-z:]*[ck]$", tl):
            payload = " ".join(args[i + 1 :]).strip()
            if len(payload) >= 2 and payload[0] == '"' and payload[-1] == '"':
                payload = payload[1:-1]
            if payload:
                analyze_script(payload, ctx.child(dialect="cmd", depth=ctx.depth + 1))
            return


_PS_VALUE_OPTS = ("-executionpolicy", "-ep", "-ex", "-windowstyle", "-w", "-inputformat", "-inp", "-if",
                  "-outputformat", "-o", "-of", "-version", "-v", "-configurationname", "-config",
                  "-custompipename", "-settingsfile", "-psconsolefile", "-workingdirectory", "-wd",
                  "-wo")
_PS_BOOL_OPTS = ("-noprofile", "-nop", "-noninteractive", "-noni", "-nologo", "-nol", "-noexit",
                 "-noe", "-sta", "-mta", "-login", "-l", "-interactive", "-i", "-nop", "-help", "-?")


def _pwsh_carrier(args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    i = 0
    sub_cwds = list(ctx.cwds)
    while i < len(args):
        t = args[i]
        tl = t.lower()
        if tl in ("-command", "-c", "-com", "-comm", "-comma", "-comman", "/c", "/command"):
            script = " ".join(args[i + 1 :])
            if script.strip() == "-":
                if piped and _SNIFF.search(ctx.full_text):
                    ctx.add("pwsh", "<stdin>", "a destructive script fed on stdin")
                return
            analyze_script(script, ctx.child(dialect="ps", depth=ctx.depth + 1, cwds=sub_cwds))
            return
        if tl in ("-encodedcommand", "-enc", "-e", "-ec", "-en", "-encoded", "-encodedc"):
            if i + 1 >= len(args):
                ctx.add("pwsh -EncodedCommand", "", "an encoded command with no payload")
                return
            try:
                raw = base64.b64decode(args[i + 1], validate=True)
                script = raw.decode("utf-16-le")
            except (ValueError, UnicodeDecodeError) as exc:
                ctx.add("pwsh -EncodedCommand", args[i + 1][:40], f"an encoded payload that cannot be decoded ({type(exc).__name__})")
                return
            analyze_script(script, ctx.child(dialect="ps", depth=ctx.depth + 1, cwds=sub_cwds))
            return
        if tl in ("-file", "-f", "-fi", "-fil"):
            if i + 1 < len(args):
                _inspect_script(args[i + 1], "ps", ctx.child(cwds=sub_cwds), "pwsh -File")
            else:
                ctx.add("pwsh -File", "", "a script carrier with no script")
            return
        if tl in ("-workingdirectory", "-wd", "-wo") and i + 1 < len(args):
            wd = _to_abs_paths(args[i + 1], ctx)
            sub_cwds = wd if wd is not None else []
            i += 2
            continue
        if tl in _PS_VALUE_OPTS:
            i += 2
            continue
        if tl.startswith("-") and len(tl) > 1:
            i += 1
            continue
        # Windows PowerShell 5.1: the first positional IS -Command.
        analyze_script(" ".join(args[i:]), ctx.child(dialect="ps", depth=ctx.depth + 1, cwds=sub_cwds))
        return


def _wsl_carrier(args: list[str], ctx: _Ctx) -> None:
    i = 0
    cwds = list(ctx.cwds)
    while i < len(args):
        t = args[i]
        tl = t.lower()
        if tl in ("--unregister", "--uninstall"):
            ctx.add("wsl " + tl, " ".join(args[i + 1 : i + 2]), "unregistering a distribution destroys its filesystem")
            return
        if tl in ("-d", "--distribution", "-u", "--user", "--shell-type", "--distribution-id"):
            i += 2
            continue
        if tl == "--cd" and i + 1 < len(args):
            target = args[i + 1]
            if target.startswith("~") or (target.startswith("/") and not target.startswith("/mnt/")):
                cwds = []
            else:
                p = _to_abs_paths(target, ctx.child(wsl=True))
                cwds = p if p is not None else []
            i += 2
            continue
        if tl in ("-e", "--exec"):
            argv = args[i + 1 :]
            if argv:
                _analyze_argv(argv, ctx.child(wsl=True, dialect="posix", cwds=cwds, depth=ctx.depth + 1))
            return
        if tl == "--":
            script = " ".join(args[i + 1 :])
            if script:
                analyze_script(script, ctx.child(wsl=True, dialect="posix", cwds=cwds, depth=ctx.depth + 1))
            return
        if tl.startswith("-"):
            if tl in ("--system",):
                i += 1
                continue
            return  # a management verb (--list, --status, --shutdown, ...)
        script = " ".join(args[i:])
        analyze_script(script, ctx.child(wsl=True, dialect="posix", cwds=cwds, depth=ctx.depth + 1))
        return


def _start_process(args: list[str], ctx: _Ctx) -> None:
    file_path: str | None = None
    arg_list: list[str] = []
    i = 0
    while i < len(args):
        t = args[i]
        tl = t.lower()
        if tl.startswith("-") and "-filepath".startswith(tl) and len(tl) >= 2 and i + 1 < len(args):
            file_path = args[i + 1]
            i += 2
            continue
        if tl.startswith("-") and ("-argumentlist".startswith(tl) or tl == "-args") and len(tl) >= 2 and i + 1 < len(args):
            arg_list.append(args[i + 1])
            i += 2
            continue
        if tl.startswith("-") or tl.startswith("/"):
            i += 1
            continue
        if file_path is None:
            file_path = t
        else:
            arg_list.append(t)
        i += 1
    if not file_path:
        return
    tokens: list[str] = [file_path]
    for chunk in arg_list:
        try:
            segs, _subs = _parse_posix(chunk.replace(",", " "))
        except _ParseError:
            ctx.add("Start-Process", chunk[:60], "an argument list that cannot be parsed")
            return
        for s in segs:
            tokens.extend(s.argv)
    _analyze_argv(tokens, ctx.child(depth=ctx.depth + 1))


def _docker(b: str, args: list[str], ctx: _Ctx) -> None:
    i = 0
    value_globals = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"}
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in value_globals else 1
    if b == "docker-compose":
        sub_args = args[i:]
        sub = "compose"
    else:
        if i >= len(args):
            return
        sub = args[i].lower()
        sub_args = args[i + 1 :]
    if sub == "container" and sub_args:
        sub = sub_args[0].lower()
        sub_args = sub_args[1:]
    if sub == "compose":
        j = 0
        value_c = {"-f", "--file", "-p", "--project-name", "--project-directory", "--env-file", "--profile"}
        while j < len(sub_args) and sub_args[j].startswith("-"):
            j += 2 if sub_args[j] in value_c else 1
        if j >= len(sub_args):
            return
        sub = sub_args[j].lower()
        sub_args = sub_args[j + 1 :]
    if sub not in ("run", "exec", "create"):
        return
    host_ctx = ctx.child(container=False)
    for j, t in enumerate(sub_args):
        tl = t.lower()
        if tl == "--privileged" or tl == "--privileged=true":
            ctx.add(b + " --privileged", "", "a privileged container can reach the host")
        val: str | None = None
        if tl in ("-v", "--volume", "--mount") and j + 1 < len(sub_args):
            val = sub_args[j + 1]
        elif tl.startswith(("--volume=", "--mount=")):
            val = t.split("=", 1)[1]
        elif tl.startswith("-v") and len(t) > 2 and not tl.startswith("--"):
            val = t[2:]
        if val is None:
            continue
        src = _mount_source(val, is_mount=tl.startswith("--mount"))
        if src is None:
            continue
        if not _scoped(src, host_ctx):
            ctx.add(b + " host mount", src, "a host bind mount outside the Claude Code session scratchpad")
    for j, t in enumerate(sub_args):
        bt = _base(t)
        if bt in ("rm", "rmdir", "unlink", "find", "shred", "truncate", "dd", "git") or bt in _SHELLS or \
                _PY_RE.match(bt) or bt in _NODE or bt in _DISK_KILLERS:
            prev = sub_args[j - 1].lower() if j else ""
            if prev in ("--name", "-w", "--workdir", "-e", "--env", "--entrypoint", "-u", "--user"):
                continue
            _analyze_argv(sub_args[j:], ctx.child(container=True, depth=ctx.depth + 1))
            return


def _mount_source(val: str, *, is_mount: bool) -> str | None:
    if is_mount:
        parts = dict(p.split("=", 1) for p in val.split(",") if "=" in p)
        src = parts.get("source") or parts.get("src")
        if not src:
            return None
        if parts.get("type", "volume") != "bind" and not re.match(r"^([/.~\\$`]|[A-Za-z]:)", src):
            return None
        return src
    m = re.match(r"^([A-Za-z]:[\\/][^:]*|[A-Za-z]:)(:|$)", val)
    src = m.group(1) if m else val.split(":", 1)[0]
    if re.match(r"^([/.~\\$`]|[A-Za-z]:)", src):
        return src
    return None  # a named volume


# ── inline interpreters ─────────────────────────────────────────────────────

_PY_LITERAL = re.compile(r"""\s*(?P<lit>(?:[rRbBuU]{0,2})(?:'[^'\\\n]*(?:\\.[^'\\\n]*)*'|"[^"\\\n]*(?:\\.[^"\\\n]*)*"))\s*(?=[,)])""")
_JS_LITERAL = re.compile(r"""\s*(?P<lit>'[^'\\\n]*(?:\\.[^'\\\n]*)*'|"[^"\\\n]*(?:\\.[^"\\\n]*)*"|`[^`$\\]*`)""")


def _py_first_literal(code: str, pos: int) -> str | None:
    m = _PY_LITERAL.match(code, pos)
    if not m:
        return None
    try:
        value = ast.literal_eval(m.group("lit"))
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _py_first_list(code: str, pos: int) -> list[str] | None:
    s = code[pos:].lstrip()
    if not s.startswith("["):
        return None
    try:
        end = _capture_balanced(s, 0, "[", "]")
        value = ast.literal_eval(s[:end])
    except (_ParseError, ValueError, SyntaxError):
        return None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return value
    return None


def _js_literal(code: str, pos: int) -> str | None:
    m = _JS_LITERAL.match(code, pos)
    if not m:
        return None
    lit = m.group("lit")
    body = lit[1:-1]
    return body.replace("\\\\", "\x00").replace("\\'", "'").replace('\\"', '"').replace("\x00", "\\")


def _run_shell_string(cmd: str, ctx: _Ctx) -> None:
    """A command STRING handed to a system shell (os.system, a subprocess
    call in shell mode, child_process.exec). This module never spawns a
    process itself; it only reads such strings. Which shell runs it depends on the host -- /bin/sh
    or cmd.exe -- and the floor must not care: it is read in BOTH dialects on
    EVERY platform. (Root cause of the VPS gate 2b failure: the cmd reading
    used to be gated on the host being Windows, so `rd /s /q repo` passed on
    Linux.)"""
    analyze_script(cmd, ctx.child(dialect="posix", depth=ctx.depth + 1))
    analyze_script(cmd, ctx.child(dialect="cmd", depth=ctx.depth + 1))


def _scan_python(code: str, ctx: _Ctx) -> None:
    _scan_code_control_writes(code, ctx, _PY_WRITE_RE, "python")
    for m in re.finditer(r"\brmtree\s*\(", code):
        lit = _py_first_literal(code, m.end())
        if lit is None:
            ctx.add("shutil.rmtree", code[m.end() : m.end() + 40], "the tree to delete cannot be bounded")
        else:
            _check_target("shutil.rmtree", lit, ctx)
    walker = re.search(r"\b(os\.walk|walk\s*\(|glob|rglob|iterdir|listdir|scandir)\b", code)
    deleter = re.search(r"\b(os\.(remove|unlink|rmdir|removedirs)\b|\.unlink\s*\(|\.rmdir\s*\(|send2trash)", code)
    if walker and deleter:
        ctx.add("python delete walker", code[:60], "a directory walk that deletes what it finds cannot be bounded")
    for m in re.finditer(
        r"\b(os\.system|os\.popen|subprocess\.(?:run|call|check_call|check_output|Popen|getoutput|getstatusoutput))\s*\(",
        code,
    ):
        lit = _py_first_literal(code, m.end())
        if lit is not None:
            _run_shell_string(lit, ctx)
            continue
        argv = _py_first_list(code, m.end())
        if argv:
            _analyze_argv(argv, ctx.child(dialect="posix", depth=ctx.depth + 1))
            continue
        if _SNIFF.search(code):
            ctx.add(m.group(1), code[m.end() : m.end() + 40], "a process launch with a destructive payload that cannot be bounded")


def _scan_node(code: str, ctx: _Ctx) -> None:
    _scan_code_control_writes(code, ctx, _NODE_WRITE_RE, "node")
    recursive = re.search(r"recursive\s*:\s*true", code) is not None
    for m in re.finditer(r"\b(rmSync|rmdirSync|rm|rmdir)\s*\(", code):
        if not recursive:
            continue
        lit = _js_literal(code, m.end())
        if lit is None:
            ctx.add("fs." + m.group(1), code[m.end() : m.end() + 40], "the tree to delete cannot be bounded")
        else:
            _check_target("fs." + m.group(1) + " (recursive)", lit, ctx)
    if re.search(r"readdirSync|readdir\s*\(|\bglob", code) and re.search(r"unlinkSync|unlink\s*\(|rmSync|\brm\s*\(", code):
        ctx.add("node delete walker", code[:60], "a directory walk that deletes what it finds cannot be bounded")
    for m in re.finditer(r"\b(execSync|exec|execFileSync|execFile|spawnSync|spawn)\s*\(", code):
        lit = _js_literal(code, m.end())
        name = m.group(1)
        if lit is None:
            if _SNIFF.search(code):
                ctx.add("child_process." + name, code[m.end() : m.end() + 40], "a process launch with a destructive payload that cannot be bounded")
            continue
        if name in ("exec", "execSync"):
            _run_shell_string(lit, ctx)
            continue
        after = code[m.end() :]
        lm = _JS_LITERAL.match(after)
        argv = [lit]
        rest = after[lm.end() :] if lm else ""
        am = re.match(r"\s*,\s*\[(.*?)\]", rest, re.S)
        if am:
            for item in re.finditer(r"""'([^'\\]*)'|"([^"\\]*)\"""", am.group(1)):
                argv.append(item.group(1) if item.group(1) is not None else item.group(2))
        _analyze_argv(argv, ctx.child(dialect="posix", depth=ctx.depth + 1))


def _python(args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    skip = False
    for i, t in enumerate(args):
        if skip:
            skip = False
            continue
        if t == "-c" and i + 1 < len(args):
            _scan_python(args[i + 1], ctx)
            return
        if t.startswith("-c") and len(t) > 2:
            _scan_python(t[2:], ctx)
            return
        if t in ("-m",):
            return  # a MODULE: resolved through sys.path, not inspected here
        if t in ("-W", "-X", "-Q"):
            skip = True
            continue
        if t == "-":
            break
        if re.match(r"^-\d+(\.\d+)?$", t):
            continue  # py launcher version selector (`py -3`)
        if not t.startswith("-"):
            _inspect_script(t, "python", ctx, "python script")  # r0b2 blocker 2
            return
    if ctx.stdin_file:
        _inspect_script(ctx.stdin_file, "python", ctx, "python < script")
        return
    if piped and _SNIFF.search(ctx.full_text):
        ctx.add("python", "<stdin>", "a destructive program fed on stdin")


def _node(b: str, args: list[str], ctx: _Ctx, *, piped: bool) -> None:
    if b == "deno" and args[:1] == ["eval"] and len(args) > 1:
        _scan_node(args[1], ctx)
        return
    runner = b in ("deno", "bun")
    if runner and args[:1] == ["run"]:
        args = args[1:]
    skip = False
    for i, t in enumerate(args):
        if skip:
            skip = False
            continue
        if t in ("-e", "--eval", "-p", "--print") and i + 1 < len(args):
            _scan_node(args[i + 1], ctx)
            return
        if t in ("-r", "--require", "--loader", "--import", "-C", "--conditions"):
            skip = True
            continue
        if not t.startswith("-"):
            ext = os.path.splitext(t.lower())[1]
            if runner and _SCRIPT_EXT.get(ext) != "node":
                return  # `bun run build`: a package.json script name, not a file
            _inspect_script(t, "node", ctx, b + " script")  # r0b2 blocker 2
            return
    if ctx.stdin_file:
        _inspect_script(ctx.stdin_file, "node", ctx, b + " < script")
        return
    if piped and _SNIFF.search(ctx.full_text):
        ctx.add(b, "<stdin>", "a destructive program fed on stdin")


_DOTNET_DELETE = re.compile(r"(?i)\[(?:System\.)?IO\.Directory\]::Delete\s*\(")


def _scan_dotnet(text: str, ctx: _Ctx) -> None:
    _scan_code_control_writes(text, ctx, _DOTNET_WRITE_RE, ".NET")
    for m in _DOTNET_DELETE.finditer(text):
        lit = _js_literal(text, m.end())
        tail = text[m.end() : m.end() + 200]
        if lit is None or "$" in lit:
            ctx.add("Directory.Delete", tail[:40], "the tree to delete cannot be bounded")
            continue
        if re.search(r"(?i),\s*\$true", tail.split(")")[0]):
            _check_target("Directory.Delete(recursive)", lit, ctx)
    if re.search(r"(?i)\.Delete\s*\(\s*\$true\s*\)", text):
        ctx.add("DirectoryInfo.Delete", "", "a recursive delete on an object whose path cannot be bounded")


# ── host-control protected paths (Edit / Write family) ──────────────────────


def _host_control_hit(raw: str, env: dict, cwd: str | None) -> str | None:
    ppc = _load_sibling("protected_paths_classifier")
    home = _home(env)
    try:
        norm = ppc.normalize_path(
            raw,
            project_root=Path(cwd) if cwd and os.path.isabs(cwd) else None,
            home_dir=Path(home),
        )
    except Exception:  # noqa: BLE001 - an unclassifiable path is not a host-control hit
        return None
    # The trailing slash makes the control DIRECTORY itself (`~/.aidocs`,
    # `.claude`) match, not only paths beneath it.
    low = norm.lower().rstrip("/") + "/"
    if "/.claude/worktrees/" in low or re.search(r"/\.claude/projects/[^/]+/memory/", low):
        return None
    for token in ppc._HOST_HARNESS_FIXED_DIRS:
        if token in low:
            return token
    home_norm = ppc.normalize_path(home).lower().rstrip("/")
    for prefix in ppc._AIDOCS_USER_PREFIXES:
        if low.startswith(home_norm + prefix):
            return prefix
    return None


#: Spellings of host-control territory in a target the floor cannot resolve
#: (`$HOME/.aidocs/...`, `F=~/.aidocs/...; ... > $F`). Deliberately NOT a bare
#: `.aidocs` -- a project's `.MEMORY/.aidocs/` is not host control.
_CONTROL_TOKEN_RE = re.compile(
    r"(?i)(?:(?:~|home\}?|userprofile%?|\$env:userprofile)[\\/]+\.aidocs"
    r"|\.aidocs[\\/]+(?:runtime|daemon)"
    r"|aidocs_(?:host_destructive_floor|shell_wrapper_peel|protected_paths_classifier|mcp_claude_hook_shim)"
    r"|\.claude[\\/]+(?!worktrees\b|projects[\\/]+[^\\/]+[\\/]+memory\b))"
)

#: Commands that only READ the paths they are given. Anything else naming a
#: host-control path is treated as a mutation of it.
_READ_ONLY_BASES = frozenset(
    {
        "cat", "type", "more", "less", "head", "tail", "grep", "egrep", "fgrep", "rg",
        "findstr", "select-string", "sls", "ls", "dir", "gci", "get-childitem", "get-item",
        "gi", "get-content", "gc", "stat", "file", "wc", "diff", "cmp", "fc", "sha256sum",
        "sha1sum", "md5sum", "cksum", "jq", "yq", "echo", "printf", "write-output",
        "write-host", "test", "[", "[[", "realpath", "readlink", "basename", "dirname",
        "test-path", "resolve-path", "get-filehash", "nl", "od", "xxd", "hexdump",
        "strings", "find", "tree", "du", "where", "which",
    }
)


def _control_target(raw: str, ctx: _Ctx) -> str | None:
    """The host-control class ``raw`` names, or None. A target that cannot
    be resolved counts as control when it -- or the command around it --
    spells control territory."""
    if not raw:
        return None
    if _unbounded(raw, ctx):
        if _CONTROL_TOKEN_RE.search(raw) or _CONTROL_TOKEN_RE.search(ctx.full_text or ""):
            return "unresolvable target in host-control territory"
        return None
    if _CONTROL_TOKEN_RE.search(raw):
        return "host-control spelling"
    paths = _to_abs_paths(raw, ctx)
    if not paths:
        return None
    cwd = ctx.cwds[0] if ctx.cwds else None
    for p in paths:
        for form in (p, _canonical(p)):
            hit = _host_control_hit(form, ctx.env, cwd)
            if hit:
                return hit
    return None


def _check_control_write(primitive: str, raw: str, ctx: _Ctx) -> bool:
    hit = _control_target(raw, ctx)
    if hit:
        ctx.add(primitive, raw, f"writes host-control territory ({hit}); only the operator may change it")
        return True
    return False


def _control_operands(b: str, args: list[str], ctx: _Ctx) -> None:
    """Any non-read-only command naming a host-control path mutates it."""
    for a in args:
        if not a or any(ch.isspace() for ch in a):
            continue  # a command STRING: analysed by its carrier instead
        cands: list[str] = []
        if a.startswith("-"):
            if "=" in a:
                cands.append(a.split("=", 1)[1])
            elif ":" in a[1:] and ctx.dialect == "ps":
                cands.append(a.split(":", 1)[1])
        else:
            cands.append(a)
            if re.match(r"^[A-Za-z_]+=", a):
                cands.append(a.split("=", 1)[1])
        for c in cands:
            if _check_control_write(b + " on a host-control path", c, ctx):
                return


def _scan_code_control_writes(code: str, ctx: _Ctx, write_re: re.Pattern[str], lang: str) -> None:
    """Inline runtime writes (python / node / .NET) aimed at host control."""
    if not write_re.search(code):
        return
    for m in re.finditer(r"'([^'\n]*)'|\"([^\"\n]*)\"", code):
        lit = m.group(1) if m.group(1) is not None else m.group(2)
        if lit and len(lit) < 1024 and _control_target(lit.replace("\\\\", "\\"), ctx):
            ctx.add(lang + " write", lit, "an inline write aimed at host-control territory")
            return
    if _CONTROL_TOKEN_RE.search(code):
        ctx.add(lang + " write", code[:60], "an inline write in code that names host-control territory")


_PY_WRITE_RE = re.compile(
    r"\bopen\s*\([^)]*['\"][rbt]*[wax+][rbt+]*['\"]|write_text|write_bytes|"
    r"shutil\.(?:copy\w*|move)|os\.(?:replace|rename|remove|unlink|symlink|link|truncate)\b|"
    r"\.touch\s*\(|\.unlink\s*\(|\.rename\s*\(|\.replace\s*\(|\.symlink_to\s*\("
)
_NODE_WRITE_RE = re.compile(
    r"writeFile|appendFile|copyFile|createWriteStream|\brename(?:Sync)?\s*\(|\bcp(?:Sync)?\s*\(|"
    r"symlink|\bunlink(?:Sync)?\s*\(|\brm(?:Sync)?\s*\(|truncate|\bopenSync\s*\([^)]*['\"][wa]"
)
_DOTNET_WRITE_RE = re.compile(
    r"(?i)\[(?:System\.)?IO\.(?:File|Directory|FileInfo)\]::(?:Write|Append|Copy|Move|Replace|"
    r"Create|Delete|Open)|\.Save\s*\("
)


def _patch_paths(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?m)^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", text or ""):
        out.append(m.group(1))
    for m in re.finditer(r"(?m)^(?:\+\+\+|---) (?:[ab]/)?(\S+)", text or ""):
        if m.group(1) != "/dev/null":
            out.append(m.group(1))
    return out


def _file_tool_findings(tool_input: dict, env: dict, cwd: str | None) -> list[Finding]:
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            paths.append(v)
    for key in ("patch", "input", "diff"):
        v = tool_input.get(key)
        if isinstance(v, str):
            paths.extend(_patch_paths(v))
    findings = []
    for p in paths:
        hit = _host_control_hit(p, env, cwd)
        if hit:
            findings.append(Finding("host-control write", p, f"a host control path ({hit}) — hooks, settings and the AIDOCS runtime"))
    return findings


# ── public surface ──────────────────────────────────────────────────────────

_DIALECT_BY_TOOL = {"bash": "posix", "monitor": "posix", "wsl": "posix", "powershell": "ps",
                    "pwsh": "ps", "cmd": "cmd"}


def analyze_command(command: str, *, tool: str = "bash", cwd: str | None = None,
                    env: dict | None = None) -> list[Finding]:
    """Every destructive finding in ``command`` as the host tool ``tool`` would
    run it from ``cwd``. Empty list == the floor has nothing to say."""
    e = _environ(env)
    tool = normalize_tool_name(tool)
    cwds = [cwd] if isinstance(cwd, str) and cwd and os.path.isabs(cwd) else []
    findings: list[Finding] = []
    ctx = _Ctx(
        env=e,
        cwds=cwds,
        dialect=_DIALECT_BY_TOOL.get(tool, "posix"),
        roots=_scratch_roots(e),
        findings=findings,
        wsl=(tool == "wsl"),
        full_text=command,
    )
    analyze_script(command, ctx)
    return findings


def is_scratch_scoped(path: str, *, cwd: str | None = None, env: dict | None = None) -> bool:
    """True when ``path`` is provably a strict child of the Claude Code
    session scratchpad (the same law the floor applies). Never raises."""
    try:
        e = _environ(env)
        ctx = _Ctx(env=e, cwds=[cwd] if cwd and os.path.isabs(cwd) else [], dialect="posix",
                   roots=_scratch_roots(e), findings=[])
        return _scoped(path, ctx)
    except Exception:  # noqa: BLE001
        return False


def _deny(reason: str, blocked_by: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
            "blocked_by": blocked_by,
        }
    }


def deny_response(findings: list[Finding], *, env: dict | None = None) -> dict:
    e = _environ(env)
    lines = [f"  - {f.primitive} -> {f.target or '(none)'}: {f.why}" for f in findings[:6]]
    root = _scratch_roots(e)[0].path
    reason = (
        "AIDOCS HOST DESTRUCTIVE FLOOR - DENIED (terminal: host_destructive_scope_floor).\n"
        + "\n".join(lines)
        + "\nA destructive primitive may only target a STRICT child of the Claude Code "
        f"session scratchpad ({root}/<project>/<session>/scratchpad/...; never the "
        "scratchpad itself, home, the cwd or an ancestor). This refusal runs before every "
        "project, session and setting layer, no file or setting widens it, and it is not "
        "confirmable. If this target really is disposable, ask the operator to delete it."
    )
    return _deny(reason, FLOOR_CLASS)


def evaluate_pretooluse(payload: object, *, env: dict | None = None) -> dict | None:
    """The floor's verdict for one hook payload: a deny response, or None."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("hook_event_name") or "").strip() != "PreToolUse":
        return None
    tool = normalize_tool_name(payload.get("tool_name"))
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) else None
    e = _environ(env)
    if tool in HOST_SHELL_TOOLS:
        command = tool_input.get("command") or tool_input.get("cmd") or tool_input.get("script")
        if not isinstance(command, str) or not command.strip():
            return None
        findings = analyze_command(command, tool=tool, cwd=cwd, env=e)
    elif tool in HOST_FILE_MUTATION_TOOLS:
        findings = _file_tool_findings(tool_input, e, cwd)
    else:
        return None
    if not findings:
        return None
    return deny_response(findings, env=e)


def exception_deny_response(payload: object, exc: BaseException) -> dict | None:
    """Item 8: a PreToolUse evaluation that RAISED must not silently permit a
    shell or file mutation. Returns a deny for those tools, None otherwise
    (other tools and events keep their historical fail-open posture)."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("hook_event_name") or "").strip() != "PreToolUse":
        return None
    if normalize_tool_name(payload.get("tool_name")) not in HOST_MUTATION_TOOLS:
        return None
    reason = (
        "AIDOCS GATE REFUSING - the PreToolUse evaluation raised "
        f"({type(exc).__name__}: {str(exc)[:200]}) before reaching a verdict. A shell or "
        "file-mutation call is DENIED rather than silently permitted when the gate cannot "
        "decide (#1135). Retry; if it persists the runtime needs attention."
    )
    return _deny(reason, EXCEPTION_CLASS)
