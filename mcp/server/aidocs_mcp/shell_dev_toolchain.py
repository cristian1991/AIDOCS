"""Governed DEV-TOOLCHAIN family (#472) — positive shape-check.

The governed shell's usability killer was that every unlisted developer
command fell to default-block. This module curates the WORKSPACE-BOUNDED,
NO-NETWORK developer loop as a positive shape-check (mirroring
``shell_readonly`` / ``shell_local_script``), so it works through the
governed shell without per-project config:

  * ``python`` / ``python3`` — a workspace ``.py`` script, ``-m`` with a
    curated module set (pytest/ruff/mypy/black/isort/coverage/unittest —
    NEVER pip: it is network), or ``--version``.
  * ``pytest`` / ``ruff`` / ``mypy`` / ``black`` / ``isort`` / ``coverage``
    with workspace-bounded args.
  * ``node --version`` / ``node --check <workspace-file>`` ONLY.
  * ``npm test`` / ``npm run <script-name>`` (no install/ci/exec).
  * ``npx --no-install <tool>`` ONLY (bare npx fetches from the registry).
  * ``cargo check|test|fmt|clippy`` (no install/run/publish).
  * ``git`` SAFE subcommands, explicitly listed with arg-shape checks:
    status/log/diff/show/add/commit, read-only ``branch`` forms, and
    ``stash list`` — NOT push/pull/fetch/clone/reset/clean/checkout, not
    ``branch -d/-D/-m``, not ``commit --no-verify/--amend``, and no
    context-changing global options (``-C``/``-c``/``--git-dir``/…).
  * ``make [-jN] [-s] <target-name-shaped-arg>...``.

REFUSED by the family (fall through to the allow-table / default):
  * any network-fetching subcommand (pip anything, npm/cargo install,
    git push/pull/fetch/clone, bare npx);
  * inline code (``python -c`` / ``node -e``), REPL/stdin forms;
  * absolute paths outside the workspace, ``~``, and ``..`` traversal;
  * shell metacharacters that survived upstream masking/splitting —
    redirection, chain operators, substitution, globs, escapes. Quotes
    are permitted (shlex-parsed; needed for commit messages and ``-k``
    expressions) — they are inert because ``$``/backtick/``*?[]``/
    ``<>``/``;&|`` are all refused on the RAW surface, so quoted text
    cannot smuggle a shell-active construct.

This is deliberately deny-side lossy: a commit message containing ``>``
or ``*`` refuses here and falls through to the operator table/default —
a usability cost, never a hole.

Wired in ``_evaluate_bash_policy_decision`` AFTER the unbypassable
phases (deny table, _JUDGE_DENYLIST, dangerous chains, command
substitution, heredoc consumer law), so ``git status && rm -rf /`` and
``pytest; curl x | sh`` die upstream and never reach this allowance.
Same verdict on raw Bash and ai_run (one shared core, doctrine XXII).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# Raw-surface characters that must never survive into a family-allowed
# invocation. Chain operators are split upstream, but redirection (`<`/`>`),
# background (`&`), substitution (`$`, backtick), subshells, globs, escapes
# and history expansion are refused here — defense in depth even where an
# upstream phase also catches them. Quotes are deliberately ABSENT (parsed
# by shlex; see module docstring for why they are inert).
_FORBIDDEN_RAW = set(";&|`$<>(){}~!*?[]\\\n\r\t")

# Leading `VAR=value ` env assignments — stripped before detection,
# mirroring bash_policy / shell_local_script.
_ENV_PREFIX_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")

_PY_SAFE_MODULES: frozenset[str] = frozenset(
    {"pytest", "ruff", "mypy", "black", "isort", "coverage", "unittest"},
)
_LINTERS: frozenset[str] = frozenset(
    {"pytest", "ruff", "mypy", "black", "isort", "coverage"},
)
_CARGO_SAFE: frozenset[str] = frozenset({"check", "test", "fmt", "clippy"})
_GIT_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "log", "diff", "show", "branch", "add", "commit", "stash"},
)
# Context-changing / hidden-write git GLOBAL options (mirrors
# shell_readonly._GIT_FORBIDDEN_TOKENS): deny anywhere in the arg list.
_GIT_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "-C",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--namespace",
    "-c",
    "--config-env",
    "--no-index",
    "--output",
    "--paginate",
    "--ext-diff",
    "--textconv",
    "--upload-pack",
)
# branch write/rename/delete forms — the family only allows LISTING shapes.
_GIT_BRANCH_FORBIDDEN: frozenset[str] = frozenset(
    {
        "-d", "-D", "-m", "-M", "-f", "-u", "--delete", "--move", "--copy",
        "--force", "--set-upstream-to", "--unset-upstream", "--edit-description",
    },
)
# commit forms that bypass hooks or rewrite history.
_GIT_COMMIT_FORBIDDEN: frozenset[str] = frozenset({"--no-verify", "-n", "--amend"})

_MAKE_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:%/-]*$")
_MAKE_JOBS_RE = re.compile(r"^-j\d{0,3}$")
_MAKE_SAFE_FLAGS: frozenset[str] = frozenset({"-s", "-k", "--silent", "--keep-going"})
_NPM_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_NPX_TOOL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@/._-]*$")


def _basename(token: str) -> str:
    t = token.strip().replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1]
    if t.lower().endswith(".exe"):
        t = t[:-4]
    return t.lower()


def _ws_token_ok(tok: str, ws: Path) -> tuple[bool, str]:
    """Workspace bound for one argv token (or an ``--opt=`` value).

    Flags are the caller's business; here every value must not use ``~``,
    ``..`` traversal, or a rooted path resolving OUTSIDE the workspace.
    Non-rooted tokens (``tests/x.py``, ``.``, ``-q``-less selectors) are
    inside the workspace by construction. Existence is NOT required —
    selectors like ``tests/x.py::name`` are legal argv data.
    """
    if not tok:
        return True, ""
    # pytest node-id selectors: bound-check only the path half.
    tok = tok.split("::", 1)[0]
    if not tok:
        return True, ""
    norm = tok.replace("\\", "/")
    if norm.startswith("~"):
        return False, f"home path '{tok}' is not workspace-bounded"
    if ".." in norm.split("/"):
        return False, f"parent traversal in '{tok}'"
    rooted = (
        norm.startswith("/")
        or (len(tok) >= 2 and tok[1] == ":")
        or Path(tok).is_absolute()
    )
    if not rooted:
        return True, ""
    try:
        rp = Path(tok).resolve()
    except Exception:
        return False, f"path '{tok}' unresolvable"
    if rp != ws and ws not in rp.parents:
        return False, f"path '{tok}' resolves outside the workspace {ws}"
    return True, ""


def _guard_args(args: list[str], ws: Path) -> tuple[bool, str]:
    """Universal workspace bound over an argv tail. ``--opt=value`` checks
    the value; bare flags pass (their SHAPE was validated per tool)."""
    for a in args:
        if a.startswith("-"):
            if "=" in a:
                ok, reason = _ws_token_ok(a.split("=", 1)[1], ws)
                if not ok:
                    return False, reason
            continue
        ok, reason = _ws_token_ok(a, ws)
        if not ok:
            return False, reason
    return True, ""


def _ws_existing_file(tok: str, ws: Path, *, suffix: str = "") -> tuple[bool, str]:
    """A PROJECT-LOCAL relative path to an existing file under the workspace
    (mirrors shell_local_script's script rules)."""
    norm = tok.replace("\\", "/")
    if (
        norm.startswith(("/", "~", "http://", "https://"))
        or (len(tok) >= 2 and tok[1] == ":")
        or Path(tok).is_absolute()
    ):
        return False, f"'{tok}' must be a project-local relative path"
    if ".." in norm.split("/"):
        return False, f"parent traversal in '{tok}'"
    if suffix and not norm.lower().endswith(suffix):
        return False, f"'{tok}' is not a {suffix} file"
    try:
        rp = (ws / tok).resolve()
    except Exception:
        return False, f"path '{tok}' unresolvable"
    if rp != ws and ws not in rp.parents:
        return False, f"'{tok}' resolves outside the workspace"
    if not rp.is_file():
        return False, f"'{tok}' is not an existing file"
    return True, ""


# ── per-tool validators ──────────────────────────────────────────────


def _v_python(args: list[str], ws: Path) -> tuple[bool, str]:
    if not args:
        return False, "bare interpreter (REPL) is not permitted"
    head = args[0]
    if head in ("--version", "-V") and len(args) == 1:
        return True, "python version probe"
    if head == "-m":
        if len(args) < 2:
            return False, "python -m without a module"
        mod = args[1]
        if mod not in _PY_SAFE_MODULES:
            return False, (
                f"python -m {mod} is not in the curated module set "
                f"({', '.join(sorted(_PY_SAFE_MODULES))}); pip is excluded (network)"
            )
        return _guard_args(args[2:], ws)
    if head.startswith("-"):
        return False, (
            f"python flag '{head}' is refused (-c inline code, -i REPL and "
            "friends are not the governed script form)"
        )
    ok, reason = _ws_existing_file(head, ws, suffix=".py")
    if not ok:
        return False, reason
    return _guard_args(args[1:], ws)


def _v_linter(args: list[str], ws: Path) -> tuple[bool, str]:
    return _guard_args(args, ws)


def _v_node(args: list[str], ws: Path) -> tuple[bool, str]:
    if args in (["--version"], ["-v"]):
        return True, "node version probe"
    if len(args) == 2 and args[0] in ("--check", "-c"):
        return _ws_existing_file(args[1], ws)
    return False, (
        "only `node --version` and `node --check <workspace-file>` are governed "
        "(script execution / -e inline code are not)"
    )


def _v_npm(args: list[str], ws: Path) -> tuple[bool, str]:
    if not args:
        return False, "bare npm"
    sub = args[0]
    if sub in ("--version", "-v") and len(args) == 1:
        return True, "npm version probe"
    if sub in ("test", "t"):
        return _guard_args(args[1:], ws)
    if sub in ("run", "run-script"):
        if len(args) < 2:
            return False, "npm run without a script name"
        if not _NPM_SCRIPT_RE.fullmatch(args[1]):
            return False, f"npm script name '{args[1]}' is not name-shaped"
        return _guard_args(args[2:], ws)
    return False, (
        f"npm {sub} is not governed (install/ci/exec and other "
        "network/registry subcommands are excluded)"
    )


def _v_npx(args: list[str], ws: Path) -> tuple[bool, str]:
    if not args or args[0] != "--no-install":
        return False, "npx is governed only with --no-install as the first argument"
    if len(args) < 2:
        return False, "npx --no-install without a tool"
    if not _NPX_TOOL_RE.fullmatch(args[1]):
        return False, f"npx tool name '{args[1]}' is not name-shaped"
    return _guard_args(args[2:], ws)


def _v_cargo(args: list[str], ws: Path) -> tuple[bool, str]:
    if not args:
        return False, "bare cargo"
    sub = args[0]
    if sub in ("--version", "-V") and len(args) == 1:
        return True, "cargo version probe"
    if sub not in _CARGO_SAFE:
        return False, (
            f"cargo {sub} is not governed (only check/test/fmt/clippy; "
            "install/run/publish are excluded)"
        )
    return _guard_args(args[1:], ws)


def _v_git(args: list[str], ws: Path) -> tuple[bool, str]:
    for a in args:
        for tok in _GIT_FORBIDDEN_TOKENS:
            if a == tok:
                return False, f"git option {tok} is not governed (context/exec escape)"
            if not tok.startswith("--") and a.startswith(tok) and len(a) > len(tok):
                return False, f"git option {tok} is not governed (context/exec escape)"
            if tok.startswith("--") and a.startswith(tok + "="):
                return False, f"git option {tok} is not governed (context/exec escape)"
    sub = ""
    sub_index = -1
    for i, a in enumerate(args):
        if a.startswith("-"):
            # Only the pager suppressors may precede the subcommand.
            if a in ("-P", "--no-pager"):
                continue
            return False, f"git pre-subcommand flag '{a}' is not governed"
        sub = a.lower()
        sub_index = i
        break
    if not sub:
        return False, "git requires a subcommand"
    if sub not in _GIT_SAFE_SUBCOMMANDS:
        return False, (
            f"git {sub} is not in the governed safe set "
            "(push/pull/fetch/clone/reset/clean/checkout fall through to policy)"
        )
    tail = args[sub_index + 1 :]
    if sub == "branch":
        for a in tail:
            if a in _GIT_BRANCH_FORBIDDEN:
                return False, f"git branch {a} writes/deletes — not governed"
    if sub == "commit":
        for a in tail:
            if a in _GIT_COMMIT_FORBIDDEN:
                return False, f"git commit {a} is not governed (hook bypass / rewrite)"
    if sub == "stash":
        heads = [a for a in tail if not a.startswith("-")]
        if not heads or heads[0].lower() != "list":
            return False, "only `git stash list` is governed (pop/apply/drop mutate)"
    return _guard_args(tail, ws)


def _v_make(args: list[str], ws: Path) -> tuple[bool, str]:
    del ws  # targets are names, never paths to bound-check
    for a in args:
        if a.startswith("-"):
            if _MAKE_JOBS_RE.fullmatch(a) or a in _MAKE_SAFE_FLAGS:
                continue
            return False, f"make flag '{a}' is not governed (-f/-C/--eval escape)"
        if not _MAKE_TARGET_RE.fullmatch(a):
            return False, f"make argument '{a}' is not target-name-shaped"
    return True, ""


_VALIDATORS = {
    "python": _v_python,
    "python3": _v_python,
    "pytest": _v_linter,
    "ruff": _v_linter,
    "mypy": _v_linter,
    "black": _v_linter,
    "isort": _v_linter,
    "coverage": _v_linter,
    "node": _v_node,
    "npm": _v_npm,
    "npx": _v_npx,
    "cargo": _v_cargo,
    "git": _v_git,
    "make": _v_make,
}


def is_governed_dev_toolchain(
    segment: str,
    workspace_root: str | Path,
) -> tuple[bool, str]:
    """True for a provably workspace-bounded, no-network dev-toolchain
    invocation. Default-deny; see the module docstring for the shape law.
    """
    s = (segment or "").strip()
    if not s:
        return False, "empty segment"
    s2 = _ENV_PREFIX_RE.sub("", s).strip()
    if not s2:
        return False, "empty after env-prefix strip"
    for ch in s2:
        if ch in _FORBIDDEN_RAW:
            return False, (
                f"metacharacter {ch!r} survived masking — redirection/chain/"
                "substitution/glob forms are not governed"
            )
    try:
        toks = shlex.split(s2)
    except ValueError:
        return False, "unparseable invocation (unbalanced quotes)"
    if not toks:
        return False, "no tokens"
    binary = _basename(toks[0])
    if "/" in toks[0].replace("\\", "/"):
        return False, "explicit binary path is not governed (bare name only)"
    validator = _VALIDATORS.get(binary)
    if validator is None:
        return False, f"'{binary}' is not in the governed dev-toolchain set"
    try:
        ws = Path(workspace_root).resolve()
    except Exception:
        return False, "workspace root unresolvable"
    ok, reason = validator(toks[1:], ws)
    if not ok:
        return False, reason
    return True, f"governed dev-toolchain: {binary}"
