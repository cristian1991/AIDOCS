"""The daemon's ONE hardened internal-git profile (#1237) + run_git_sync.

Every AIDOCS-internal git spawn (status / diff / add / commit / rev-parse /
log ... on a project checkout) goes through this module. The checkout is
agent-writable, so anything git would execute or redirect on the agent's
say-so (.git/hooks/*, core.fsmonitor, core.worktree, diff.external,
textconv, gpg.program, editors, filter/merge drivers, pager, remote helpers)
must be inert or refused when the daemon runs git there.

Public API (argv-form callers, e.g. a future ai_git wiring):

    argv, env = hardened_git_call(args, cwd=<dir git runs in>)
    subprocess.run(argv, cwd=<same dir>, env=env, ...)

``args`` is the git argv WITHOUT the leading ``git`` (list of str, no shell
string). ``cwd`` is the directory the caller will run git in; ``-C`` inside
``args`` is honoured relative to it. ``hardened_git_call`` raises
``GitProfileRefusal`` (a RuntimeError) instead of returning when the repo
config would make git run something repo-controlled. ``hardened_git_argv`` /
``hardened_git_env`` are the two halves without the config probe (use them
only for verbs that cannot run repo programs, or after your own probe).

Profile (argv, inserted immediately BEFORE the subcommand so it is the last
word over any caller-supplied global ``-c``):

    git --no-pager [caller globals]
        [-c safe.directory=<exact worktree root>]   only when one is found
        -c core.hooksPath=<null device: /dev/null | //./NUL>
        -c core.fsmonitor=false
        -c core.pager=cat
        -c diff.external=
        -c commit.gpgSign=false -c tag.gpgSign=false -c log.showSignature=false
        <verb> [--no-verify]                         commit
               [--no-ext-diff --no-textconv]         diff/show/log family
        [caller args]

Hooks root: a path under which no hook file can ever exist. On POSIX
``/dev/null`` (``/dev/null/pre-commit`` is ENOTDIR). On Windows the
device-namespace spelling ``//./NUL``; the bare ``NUL`` / ``os.devnull``
spelling is RELATIVE to git's cwd and a same-UID process can create a real
directory named NUL there (``\\\\?\\`` prefix) whose hooks git then finds. A
platform with neither refuses rather than falling back to a writable dir.

Repo-controlled config probe (ONE ``git config --show-scope --get-regexp``
per invocation, includes resolved): keys from local / worktree / command
scope are refused for the verbs where they can act, unless an identical
key=value is set in system/global (operator) scope:

    core.worktree            worktree verbs; must resolve to the discovered root
    filter.*.clean/smudge/process, merge.*.driver   filter/merge-capable verbs
    core.editor, sequence.editor                    commit/merge/rebase/...
    gpg.program, gpg.*.program                      commit/tag/log/show/...
    core.sshCommand, credential[.*].helper          remote verbs (incl. #1214's
                                                    fetch/push/pull/clone/ls-remote)
    core.askPass, core.gitProxy, uploadpack.packObjectsHook,
    protocol[.*].allow=always                       submodule / remote update|prune|show
                                                    (left to #1214 on transport verbs)

core.worktree is refused, not pinned: a ``-c core.worktree=`` travels in
GIT_CONFIG_PARAMETERS into every child git (submodule status recursion), which
would point each submodule at the superproject tree; bare repos would also be
turned non-bare. Refusing an off-root value keeps both shapes correct.

#1214's credential/sterile transport (gate_git_transport, sterile_git_stage)
keeps its own custody profile and is not routed here.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

__all__ = [
    "GitProfileRefusal",
    "git_worktree_root",
    "hardened_git_argv",
    "hardened_git_call",
    "hardened_git_env",
    "program_driver_refusal",
    "run_git_sync",
]

# Windows: the daemon runs console-less (pythonw). Without this flag every
# subprocess spawn allocates a NEW visible console window (#333 Phase 2).
_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_GIT = "git"

# Global options that consume the FOLLOWING argv element.
_GLOBAL_OPTS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})

# Verbs that read objects/refs/config only and never run a worktree clean/
# smudge filter or merge driver (textconv/external diff are disabled by flag).
_NO_FILTER_VERBS = frozenset({
    "rev-parse", "cat-file", "config", "rev-list", "symbolic-ref", "remote",
    "init", "ls-tree", "show-ref", "for-each-ref", "merge-base", "describe",
    "write-tree", "log", "show", "version", "var", "check-ref-format",
    "name-rev", "ls-remote", "fetch", "push", "clone", "count-objects",
    "submodule",
})
_DIFF_VERBS = frozenset({
    "diff", "show", "log", "whatchanged", "format-patch",
    "diff-tree", "diff-index", "diff-files",
})
# Transport verbs belong to #1214: their ssh/askpass env is left untouched.
_TRANSPORT_VERBS = frozenset({"fetch", "pull", "push", "ls-remote", "clone"})
# Verbs that never consult the worktree (core.worktree cannot redirect them).
_NO_WORKTREE_VERBS = frozenset({"config", "version", "init", "clone", "ls-remote", "fetch", "push"})
_EDITOR_VERBS = frozenset({
    "commit", "merge", "pull", "rebase", "tag", "revert", "cherry-pick", "notes", "am",
})
_GPG_VERBS = frozenset({
    "commit", "tag", "merge", "pull", "rebase", "cherry-pick", "revert", "am",
    "log", "show", "verify-commit", "verify-tag",
})
_REMOTE_SUBCOMMANDS = frozenset({"update", "prune", "show"})

# ONE combined regex; git matches it against canonical (lower-cased section
# and variable) key names.
_PROGRAM_KEY_REGEX = (
    r"^(core\.worktree"
    r"|filter\..*\.(clean|smudge|process)|merge\..*\.driver"
    r"|core\.(editor|sshcommand|askpass|gitproxy)|sequence\.editor"
    r"|gpg\.program|gpg\..*\.program"
    r"|credential\.helper|credential\..*\.helper"
    r"|uploadpack\.packobjectshook|protocol\.allow|protocol\..*\.allow)$"
)
_TRUSTED_SCOPES = frozenset({"system", "global"})

# Inherited env that would redirect which repository/index/config/binaries
# git uses, or make it execute or write somewhere on the env's say-so.
_STRIP_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_EXEC_PATH", "GIT_EXTERNAL_DIFF", "GIT_TEMPLATE_DIR", "GIT_ATTR_SOURCE",
    "GIT_REPLACE_REF_BASE", "GIT_REDIRECT_STDIN", "GIT_REDIRECT_STDOUT",
    "GIT_REDIRECT_STDERR",
})
_STRIP_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE")
_STRIP_TRANSPORT_ENV = frozenset({
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT", "GIT_PROXY_COMMAND",
    "GIT_ASKPASS", "SSH_ASKPASS",
})


class GitProfileRefusal(RuntimeError):
    """The hardened profile refused to run git (#1237)."""


def _null_hooks_path() -> str:
    """The platform null device as a hooks root no hook file can live under."""
    if os.name == "nt":
        return "//./NUL"
    if os.name == "posix" and os.path.exists("/dev/null") and not os.path.isdir("/dev/null"):
        return "/dev/null"
    raise GitProfileRefusal(
        "#1237 refused git: no null device usable as an inert core.hooksPath "
        "on this platform (no writable-directory fallback by design)"
    )


def _split_globals(args) -> tuple[list[str], str | None, list[str]]:
    """(caller global options, verb or None, args after the verb)."""
    args = [str(a) for a in args]
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return args[:i], a, args[i + 1:]
    return args, None, []


def git_worktree_root(cwd, globals_: list[str]) -> str | None:
    """The exact path git compares against safe.directory, or None.

    ``cwd`` is the directory git runs in; ``-C`` values in ``globals_`` are
    applied relative to it, as git does.
    """
    try:
        base = Path(cwd) if cwd else Path.cwd()
        for i, a in enumerate(globals_):
            if a == "-C" and i + 1 < len(globals_):
                base = base / globals_[i + 1]
        base = base.resolve()
    except (OSError, ValueError):
        return None
    for p in (base, *base.parents):
        try:
            if (p / ".git").exists():
                return p.as_posix()
            if (p / "HEAD").is_file() and (p / "objects").is_dir() and (p / "refs").is_dir():
                return p.as_posix()  # bare repository / git dir
        except OSError:
            return None
    return None


def _git_dir_of(root: str) -> Path:
    """The git dir core.worktree is relative to, for a discovered root."""
    r = Path(root)
    dot = r / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        line = dot.read_text(encoding="utf-8", errors="replace").strip()
        if line.lower().startswith("gitdir:"):
            g = Path(line.split(":", 1)[1].strip())
            return g if g.is_absolute() else r / g
    return r  # bare repository: the root IS the git dir


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def hardened_git_argv(args, *, cwd=None) -> list[str]:
    """Full argv (leading ``git``) for one internal git call under the profile.

    ``args`` excludes the leading ``git``; ``cwd`` is the directory the caller
    runs git in (used to find the exact safe.directory root). Does NOT probe
    repo config: use ``hardened_git_call`` unless you have probed already.
    """
    globals_, verb, rest = _split_globals(args)
    profile: list[str] = []
    root = git_worktree_root(cwd, globals_)
    if root:
        profile += ["-c", f"safe.directory={root}"]
    profile += [
        "-c", f"core.hooksPath={_null_hooks_path()}",
        "-c", "core.fsmonitor=false",
        "-c", "core.pager=cat",
        "-c", "diff.external=",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "log.showSignature=false",
    ]
    if verb is None:
        # No subcommand (e.g. ``--version``): option-only invocations treat
        # trailing ``-c`` as their own switch, so the profile goes first.
        return [_GIT, "--no-pager", *profile, *globals_]
    verb_flags: list[str] = []
    if verb == "commit":
        verb_flags.append("--no-verify")
    if verb in _DIFF_VERBS:
        verb_flags += ["--no-ext-diff", "--no-textconv"]
    return [_GIT, "--no-pager", *globals_, *profile, verb, *verb_flags, *rest]


def hardened_git_env(base=None, *, args=None) -> dict[str, str]:
    """Inherited env minus every git redirection; pager/prompt pinned.

    Pass the same ``args`` as to ``hardened_git_argv`` so transport verbs keep
    their ssh/askpass env (#1214's custody). GIT_CONFIG_GLOBAL /
    GIT_CONFIG_SYSTEM / GIT_CONFIG_NOSYSTEM are KEPT: they select
    operator-owned host config (identity, LFS), not anything the agent
    controls, and every profile ``-c`` outranks those files anyway.
    """
    env = dict(os.environ if base is None else base)
    _, verb, _ = _split_globals(args or [])
    for key in list(env):
        if key in _STRIP_ENV or key.startswith(_STRIP_ENV_PREFIXES):
            env.pop(key, None)
        elif key in _STRIP_TRANSPORT_ENV and verb not in _TRANSPORT_VERBS:
            env.pop(key, None)
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _key_class(key: str) -> str:
    if key == "core.worktree":
        return "worktree"
    if key.startswith("filter.") or key.startswith("merge."):
        return "driver"
    if key in ("core.editor", "sequence.editor"):
        return "editor"
    if key.startswith("gpg."):
        return "gpg"
    if key == "core.sshcommand" or key.startswith("credential."):
        return "remote_auth"
    if key.startswith("protocol."):
        return "protocol"
    return "remote_other"  # core.askpass, core.gitproxy, uploadpack.packobjectshook


def _classes_for(verb: str | None, rest: list[str]) -> frozenset[str]:
    if verb is None:
        return frozenset()
    classes: set[str] = set()
    if verb not in _NO_WORKTREE_VERBS:
        classes.add("worktree")
    if verb == "cat-file":
        if any(a in ("--filters", "--textconv") for a in rest):
            classes.add("driver")
    elif verb not in _NO_FILTER_VERBS:
        classes.add("driver")
    if verb in _EDITOR_VERBS:
        classes.add("editor")
    if verb in _GPG_VERBS:
        classes.add("gpg")
    net = verb == "submodule" or (
        verb == "remote" and bool(set(rest) & _REMOTE_SUBCOMMANDS)
    )
    if net or verb in _TRANSPORT_VERBS:
        classes.add("remote_auth")
    if net:  # transport verbs leave these to #1214
        classes.update({"remote_other", "protocol"})
    return frozenset(classes)


def program_driver_refusal(cwd, args) -> str:
    """'' when no repository-controlled program/worktree key applies, else why.

    One ``git config`` call; see the module docstring for the key/verb table.
    """
    globals_, verb, rest = _split_globals(args)
    classes = _classes_for(verb, rest)
    if not classes:
        return ""
    probe = [a for a in globals_ if a != "--no-pager"]
    try:
        out = run_git_sync(
            cwd, *probe, "config", "-z", "--show-scope", "--get-regexp", _PROGRAM_KEY_REGEX,
        )
    except RuntimeError as exc:
        if str(exc).strip() == "git exited with code 1":
            return ""  # no such key anywhere
        return f"#1237 refused git {verb}: cannot enumerate repo program keys ({exc})"
    trusted: set[tuple[str, str]] = set()
    untrusted: list[tuple[str, str, str]] = []
    fields = out.split("\x00")
    for i in range(0, len(fields) - 1, 2):
        scope, kv = fields[i].strip(), fields[i + 1]
        key, _, value = kv.partition("\n")
        key = key.lower()
        if not value.strip():
            continue
        cls = _key_class(key)
        if scope in _TRUSTED_SCOPES:
            trusted.add((key, value))
        elif cls in classes:
            untrusted.append((cls, key, value))
    root = git_worktree_root(cwd, globals_)
    bad: set[str] = set()
    for cls, key, value in untrusted:
        if cls == "worktree":
            if root is None:
                bad.add(key)
                continue
            target = Path(value)
            if not target.is_absolute():
                target = _git_dir_of(root) / target
            try:
                resolved = target.resolve().as_posix()
            except (OSError, ValueError):
                bad.add(key)
                continue
            if not _same_path(resolved, root):
                bad.add(key)
        elif cls == "protocol":
            if value.strip().lower() == "always" and (key, value) not in trusted:
                bad.add(key)
        elif (key, value) not in trusted:
            bad.add(key)
    if bad:
        return (
            f"#1237 refused git {verb}: repository-controlled config would run or "
            f"redirect git as the daemon ({', '.join(sorted(bad))})"
        )
    return ""


def hardened_git_call(args, *, cwd) -> tuple[list[str], dict[str, str]]:
    """(argv, env) for one internal git spawn; raises GitProfileRefusal.

    The single entry point every direct git site uses, so the argv
    hardening, the env scrub and the repo-config refusal can never be
    applied piecemeal. ``args`` excludes the leading ``git``; ``cwd`` is the
    directory the caller will run git in (``-C`` in ``args`` is relative to
    it). Run the returned argv with that same cwd and the returned env.
    """
    args = [str(a) for a in args]
    refusal = program_driver_refusal(cwd, args)
    if refusal:
        raise GitProfileRefusal(refusal)
    return hardened_git_argv(args, cwd=cwd), hardened_git_env(args=args)


def run_git_sync(cwd: str, *args: str, timeout: int = 10) -> str:
    argv, env = hardened_git_call(args, cwd=cwd)
    out_path = err_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".out", delete=False) as handle:
            out_path = handle.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".err", delete=False) as handle:
            err_path = handle.name
        with (
            open(out_path, "w", encoding="utf-8") as out_fh,
            open(err_path, "w", encoding="utf-8") as err_fh,
        ):
            # #345: routed through audited_run (ledger row per spawn).
            # Passthrough lambda IS the registered AST callsite.
            from .shell_egress_service import audited_run

            result = audited_run(
                argv,
                fingerprint=("git_helpers.py", "run_git_sync", "subprocess.run"),
                reason="git-porcelain-helper",
                run=lambda *a, **kw: subprocess.run(*a, **kw),
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_fh,
                stderr=err_fh,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=_WIN_NO_WINDOW,
            )
        stdout = Path(out_path).read_text(encoding="utf-8", errors="ignore").strip()
        stderr = Path(err_path).read_text(encoding="utf-8", errors="ignore").strip()
    finally:
        for path in (out_path, err_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    if result.returncode != 0:
        message = (stderr or stdout or f"git exited with code {result.returncode}").strip()
        raise RuntimeError(message)
    return stdout
