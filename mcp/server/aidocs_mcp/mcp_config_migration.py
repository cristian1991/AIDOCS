"""Migrate EXISTING projects' aidocs host entries off pinned runtimes (#1202 follow-up).

THE DEFECT. #1202 made a NEW ``.mcp.json`` aidocs entry name the static stdio
shim (``~/.aidocs/runtime/aidocs_mcp_stdio_shim.py`` under the stable
interpreter, ``-E -s -S``). Every project set up BEFORE it still names a
runtime generation (``...\\generations\\gen-...\\venv\\Scripts\\python.exe -m
aidocs_mcp.stdio_shim``) or the legacy venv. Those entries work until the
generation is pruned, and then the window silently loses AIDOCS. A manual
per-project ``aidocs setup`` was rejected by the operator.

THE CURE (r0b2 design, binding):

1. EXACT FINGERPRINTS ONLY. An entry is rewritten only when it is byte-for-byte
   a shape AIDOCS itself wrote (see ``_CLAUDE_MODULES`` / ``_oc_legacy``):
   an AIDOCS-owned volatile interpreter (a generation venv or the legacy
   runtime venv under ``<home>/.aidocs/runtime``) running ``aidocs_mcp.
   stdio_shim`` / ``aidocs_mcp.mcp_server`` with exactly the args AIDOCS
   wrote, for THIS project's root. Anything else -- hand-edited, unknown,
   extra args, extra keys -- is left alone and reported.
2. THE REGISTRY IS DISCOVERY, NOT AUTHORITY. Each candidate root is re-checked:
   absolute, real (no symlink / junction / reparse point on the root or ANY
   ancestor), not a protected home path, adopted (``_require_adopted``); the
   file is a regular, non-linked, single-link file inside the real root.
3. COMPARE-AND-SWAP. Read bytes + identity, change ONLY the aidocs entry (other
   servers, top-level keys and operator env keys survive), temp file in the
   same directory, then move the live file aside as the backup, verify it is
   exactly what was read, install the new file with a no-replace primitive and
   re-verify (see ``_cas_replace`` for what is atomic and what is detected).
   A file that changed is skipped and put back (the user's edit wins). A
   missing file is never created; non-plain JSON is refused. The SQL
   ``mcp_servers`` row settles with the file: ownership-checked first
   (``skipped_conflict``), compare-and-set after, the file swap undone if SQL
   fails (``error_sql_failed``).
4. WHERE. ``boot_doctor_check`` (daemon boot) is READ-ONLY. The writing
   ``sweep_project_host_configs`` runs once after a generation promotion
   (``runtime --reconcile-hook-shim``) and on ``aidocs runtime --fix``,
   bounded by a project cap and a time budget.
5. OBSERVABILITY. Per-project statuses (``rewritten`` / ``current`` / ``stale``
   (check mode) / ``absent`` / ``skipped_unknown_shape`` /
   ``skipped_concurrent_edit`` / ``skipped_conflict`` / ``error_sql_failed`` /
   ``rewritten_sql_failed`` / ``refused_<reason>``) go to the doctor report
   and the reconcile log; a sweep that did not reach every project is
   ``status='partial'`` and never ok. Nothing is model-visible.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import Callable
from pathlib import Path

from . import runtime_generations as rg
from . import runtime_launchers as rl

_log = logging.getLogger("aidocs.mcp_config_migration")

#: The one line the reconcile prints and the runtime refresh relays to its log.
SUMMARY_PREFIX = "project host configs:"

DEFAULT_MAX_PROJECTS = 50
DEFAULT_TIME_BUDGET_S = 20.0

#: Legacy Claude modules, with the commits that wrote them:
#:   aidocs_mcp.mcp_server -- direct-stdio, 2d8948071 (2026-04-07) through
#:                            7ba4ec947 (2026-06-05, one-writer seal)
#:   aidocs_mcp.stdio_shim -- daemon mode via the shim, 292e04fa4 (2026-08-17, #787)
_CLAUDE_MODULES = ("aidocs_mcp.mcp_server", "aidocs_mcp.stdio_shim")
_CLAUDE_KEYS = {"type", "command", "args", "env"}
#: OpenCode project entry, 69b5b7c21 (2026-10-20): [python, -m, stdio_shim].
_OC_KEYS = {"type", "enabled", "timeout", "command", "environment"}
_OC_TIMEOUT = 120000

_PY_TAIL = r"(?:Scripts/pythonw?\.exe|bin/python(?:3(?:\.[0-9]+)?)?)"

Target = Callable[[Path], dict]


class _Refused(Exception):
    def __init__(self, status: str, detail: str = "") -> None:
        super().__init__(detail or status)
        self.status = status
        self.detail = detail


# ── path / identity checks ────────────────────────────────────────────────────


def _nt() -> bool:
    return os.name == "nt"


def _norm(p: str | Path) -> str:
    s = os.path.normpath(str(p)).replace("\\", "/")
    return s.lower() if _nt() else s


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _protected(home: Path) -> list[str]:
    h = _norm(home)
    return [h + "/.config", h + "/.aidocs", h + "/.claude"]


def _check_root(root: Path, home: Path) -> Path:
    """The verified real root, or raise _Refused. Reads only."""
    if not root.is_absolute():
        raise _Refused("refused_not_absolute", str(root))
    parts = Path(os.path.normpath(str(root))).parts
    cur = Path(parts[0])
    for part in parts[1:]:
        cur = cur / part
        if not os.path.lexists(cur):
            raise _Refused("refused_not_a_directory", str(cur))
        if _is_link(cur):
            raise _Refused("refused_link", f"{cur} is a symlink, junction or reparse point")
    real = Path(os.path.realpath(root))
    if _norm(real) != _norm(root):
        raise _Refused("refused_link", f"{root} resolves to {real}")
    if not stat.S_ISDIR(os.lstat(real).st_mode):
        raise _Refused("refused_not_a_directory", str(real))
    r = _norm(real)
    h = _norm(home)
    if r == h or h.startswith(r.rstrip("/") + "/") or any(r == p or r.startswith(p + "/") for p in _protected(home)):
        raise _Refused("refused_protected_path", str(real))
    from ._sqlite_index_store_base import ProjectNotAdopted, _require_adopted

    try:
        _require_adopted(real)
    except ProjectNotAdopted as exc:
        raise _Refused("refused_not_adopted", str(exc)) from exc
    return real


def _ident(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _read_file(path: Path, real_root: Path) -> tuple[bytes, tuple] | None:
    """(bytes, identity) of a regular single-link file inside ``real_root``;
    None when absent; raise _Refused otherwise."""
    if not os.path.lexists(path):
        return None
    if _is_link(path):
        raise _Refused("refused_link_file", str(path))
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise _Refused("refused_not_regular_file", str(path))
    if st.st_nlink != 1:
        raise _Refused("refused_hardlink", f"{path} has {st.st_nlink} links")
    if _norm(Path(os.path.realpath(path)).parent) != _norm(real_root):
        raise _Refused("refused_outside_root", str(path))
    try:
        with open(path, "rb") as fh:
            fst = os.fstat(fh.fileno())
            raw = fh.read()
    except OSError as exc:
        raise _Refused("refused_unreadable", str(exc)) from exc
    if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino) or len(raw) != st.st_size:
        raise _Refused("skipped_concurrent_edit", "the file changed while it was read")
    return raw, _ident(st)


def _parse(raw: bytes) -> dict:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _Refused("refused_not_plain_json", "UTF-8 BOM")

    def pairs(items: list) -> dict:
        keys = [k for k, _ in items]
        if len(keys) != len(set(keys)):
            raise _Refused("refused_duplicate_keys", "a JSON object repeats a key")
        return dict(items)

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)
    except _Refused:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise _Refused("refused_not_plain_json", type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise _Refused("refused_not_object", "top level is not a JSON object")
    return data


# ── fingerprints ──────────────────────────────────────────────────────────────


def _owned_volatile_python(command: object, home: Path) -> bool:
    """An AIDOCS-owned interpreter that GOES STALE: a generation venv or the
    legacy runtime venv, exactly where AIDOCS installs them."""
    if not isinstance(command, str) or not os.path.isabs(command):
        return False
    rt = re.escape(_norm(rg.runtime_root(home)))
    gen = rf"{rg.GENERATIONS_DIRNAME}/{rg.CURRENT_GENERATION_ID_PATTERN}/"
    pat = rf"{rt}/(?:{gen})?{rg.LEGACY_VENV_DIRNAME}/{_PY_TAIL}"
    return re.fullmatch(pat, _norm(command), flags=re.IGNORECASE if _nt() else 0) is not None


def _env_ok(env: object, root: Path, *, module: str, key_endpoint: bool) -> bool:
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return False
    pr = env.get("AIDOCS_PROJECT_ROOT")
    if pr is None:
        # pre-7ba4ec947 PYTHONPATH mode dropped AIDOCS_PROJECT_ROOT entirely.
        return module == "aidocs_mcp.mcp_server" and "PYTHONPATH" in env
    if _norm(pr) != _norm(root):
        return False
    return not key_endpoint or "AIDOCS_MCP_ENDPOINT" in env


def _claude_legacy(entry: object, root: Path, home: Path) -> bool:
    if not isinstance(entry, dict) or set(entry) != _CLAUDE_KEYS or entry.get("type") != "stdio":
        return False
    args = entry.get("args")
    if not isinstance(args, list) or len(args) != 2 or args[0] != "-m" or args[1] not in _CLAUDE_MODULES:
        return False
    if not _owned_volatile_python(entry.get("command"), home):
        return False
    shim = args[1] == "aidocs_mcp.stdio_shim"
    return _env_ok(entry.get("env"), root, module=args[1], key_endpoint=shim)


def _oc_legacy(entry: object, root: Path, home: Path) -> bool:
    if not isinstance(entry, dict) or set(entry) != _OC_KEYS:
        return False
    if entry.get("type") != "local" or entry.get("enabled") is not True or entry.get("timeout") != _OC_TIMEOUT:
        return False
    cmd = entry.get("command")
    if not isinstance(cmd, list) or len(cmd) != 3 or cmd[1:] != ["-m", "aidocs_mcp.stdio_shim"]:
        return False
    if not _owned_volatile_python(cmd[0], home):
        return False
    return _env_ok(entry.get("environment"), root, module="aidocs_mcp.stdio_shim", key_endpoint=True)


#: The only endpoint a user-scope AIDOCS entry ever named: the local daemon.
_LOOPBACK_ENDPOINT = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/mcp")


def _loopback_endpoint_ok(value: object) -> bool:
    """A loopback AIDOCS endpoint on a real TCP port, 1 to 65535."""
    if not isinstance(value, str):
        return False
    m = _LOOPBACK_ENDPOINT.fullmatch(value)
    return m is not None and 1 <= int(m.group(1)) <= 65535


def _claude_user_legacy(entry: object, home: Path) -> bool:
    """The exact USER-scope shape AIDOCS ran: an owned volatile interpreter,
    ``-m aidocs_mcp.stdio_shim`` and ONLY a loopback endpoint in env. No
    AIDOCS_PROJECT_ROOT: a user-scope entry belongs to no project."""
    if not isinstance(entry, dict) or set(entry) != _CLAUDE_KEYS or entry.get("type") != "stdio":
        return False
    if entry.get("args") != ["-m", "aidocs_mcp.stdio_shim"]:
        return False
    if not _owned_volatile_python(entry.get("command"), home):
        return False
    env = entry.get("env")
    return isinstance(env, dict) and set(env) == {"AIDOCS_MCP_ENDPOINT"} and _loopback_endpoint_ok(env["AIDOCS_MCP_ENDPOINT"])


def _argv_eq(a: list, b: list) -> bool:
    return [_norm(x) for x in a] == [_norm(x) for x in b]


def _stable_argv(argv: list, home: Path) -> bool:
    """Is this argv the static shim or the generation-following launcher form?
    The migration never writes anything else."""
    try:
        if _argv_eq(argv, rl.static_shim_argv(home)):
            return True
    except rg.CurrentGenerationError:
        pass
    prefix = rl.launch_prefix(home)
    return rl.launchers_current(home) and _argv_eq(argv[: len(prefix)], prefix) and "generations" not in _norm(" ".join(argv))


class _Host:
    def __init__(self, name: str, filename: str, container: str, env_key: str, legacy, argv_of) -> None:
        self.name = name
        self.filename = filename
        self.container = container
        self.env_key = env_key
        self.legacy = legacy
        self.argv_of = argv_of


_CLAUDE = _Host(
    "claude", ".mcp.json", "mcpServers", "env", _claude_legacy,
    lambda e: [e.get("command"), *(e.get("args") or [])],
)
_OPENCODE = _Host(
    "opencode", "opencode.json", "mcp", "environment", _oc_legacy,
    lambda e: list(e.get("command") or []),
)


def _merge(old: dict, target: dict, host: _Host) -> dict:
    """``target`` with the operator's env keys kept. AIDOCS keys come from the
    target; an equivalent spelling of this root is kept as written."""
    new = json.loads(json.dumps(target))
    old_env = old.get(host.env_key) if isinstance(old.get(host.env_key), dict) else {}
    env = {**old_env, **(new.get(host.env_key) or {})}
    pr_old = old_env.get("AIDOCS_PROJECT_ROOT")
    if isinstance(pr_old, str) and _norm(pr_old) == _norm(env.get("AIDOCS_PROJECT_ROOT", "")):
        env["AIDOCS_PROJECT_ROOT"] = pr_old
    new[host.env_key] = env
    return new


# ── the swap ──────────────────────────────────────────────────────────────────
#
# WHAT IS ATOMIC AND WHAT IS ONLY DETECTED (r0b2 block 1). Neither Windows nor
# portable POSIX has "replace this file only if it is unchanged". So the live
# file is never overwritten at all:
#   1. MOVE ASIDE. Reserve a fresh backup name atomically (O_CREAT|O_EXCL
#      sentinel -- two sweeps can never share one), then rename the live file
#      onto that name only (``_move_onto_reservation``). On Windows the rename
#      fails while another process holds the file open without delete sharing
#      (most editors): the migration then skips.
#   2. VERIFY. The moved file must be the very inode that was read, with the
#      very bytes that were read, and the directory must still be the real,
#      link-free root. Otherwise it is put back and the project is skipped
#      (an edit) or refused (a link swapped onto the root or an ancestor).
#   3. INSTALL with a NO-REPLACE primitive (Windows rename; POSIX link): a
#      file that appeared at the name meanwhile wins and stays live.
#   4. RE-VERIFY the backup and the directory. A write that landed on the
#      moved-aside inode through an already-open handle, or a link swapped in,
#      undoes the install (only while the live file is still exactly ours) and
#      puts the user's file back.
# LIMIT: a write through a handle opened before step 1 that lands AFTER step 4
# goes to the backup file, which is kept on disk and never deleted -- no user
# byte is destroyed, but it is not live; that case is not detected. On POSIX
# the move-aside rename replaces our own sentinel, verified by identity just
# before; a NON-cooperating process that swaps that sentinel for another file
# in between could still be replaced (no portable RENAME_NOREPLACE); two
# sweeps cannot, since neither ever renames onto a name it did not reserve.
# On Windows the sentinel is removed and the rename refuses any file. No lock is
# taken: the other writer (McpRegistryStore.project_to_file) takes none.
#
# PINNED (VPS Gate 2b on a4c9f4704; reviews of e20aeb47a and 47fa6af53). Every
# name the swap creates, renames, links or unlinks -- the temp file, the
# reservation, the live file, the backup, the undo aside -- is resolved in the
# ONE directory that was verified, never through a path that can have been
# rebound since. Otherwise a root (or ancestor) renamed away and replaced by a
# link to a look-alike tree turns "unlink our temp" into "unlink the file with
# that name over there".
#   POSIX: ``_FdDir`` -- an O_DIRECTORY|O_NOFOLLOW fd, identity-checked against
#     the verified root; every operation is ``*at()`` relative to it, so a
#     rename of the directory or a link at its path redirects nothing. No
#     fd-relative primitives -> ``refused_no_dir_fd``: never a path fallback.
#   Windows: ``_HandleDir`` -- a directory handle WITHOUT FILE_SHARE_DELETE.
#     While it is open NTFS refuses to rename or remove the directory (sharing
#     violation) and any ancestor (access denied), so no junction can be
#     swapped onto the path at all; the path is re-verified after the handle
#     is taken, and path operations inside it are unaffected. Pin failure ->
#     ``refused_unpinnable``.
# The path checks (``_dir_ok``) still run: a swap that is detected is refused.
#
# A REFUSAL NEVER DELETES A RESERVATION. Removing "our" empty sentinel is a
# check followed by a separate unlink, and a process with write access to the
# directory could put its own file at the name in between. So a refusal leaves
# it -- an EMPTY file, no user byte. With the pin it strands only when the
# move-aside rename itself fails (POSIX), or when the Windows unlink-then-
# rename never reached the rename; a link swap no longer strands one, since
# the rename runs in the pinned directory and consumes it. BOUND: at
# ``_MAX_STRANDED_RESERVATIONS`` such files next to a config, the migration
# refuses (``refused_stranded_reservations``) instead of adding another.
# The temp file IS removed by name on a refusal: it is our own O_EXCL file in
# the pinned directory, and only a party that can already write the operator's
# real directory could have replaced it. Rejected alternatives: releasing the
# sentinel before the POSIX rename as Windows does (POSIX rename REPLACES, so
# two sweeps sharing a name lose the original again -- the 68c75524c defect);
# a hard-link reservation carrying the live bytes (the move-aside then needs an
# unlink of the live name, which can destroy a save landing in between).


def _no_links(p: Path) -> bool:
    """No symlink / junction / reparse point on ``p`` or any ancestor."""
    parts = Path(os.path.normpath(str(p))).parts
    cur = Path(parts[0])
    for part in parts[1:]:
        cur = cur / part
        if _is_link(cur):
            return False
    try:
        return _norm(os.path.realpath(p)) == _norm(p)
    except OSError:
        return False


def _dir_ok(d: Path, root_ident: tuple) -> bool:
    if not _no_links(d):
        return False
    try:
        st = os.lstat(d)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == root_ident


#: Windows ``os.rename`` is MoveFileEx WITHOUT MOVEFILE_REPLACE_EXISTING: it
#: raises FileExistsError rather than replace a destination. POSIX rename(2)
#: silently replaces it. (A module constant so a test can force POSIX
#: semantics on Windows.)
_RENAME_REFUSES_EXISTING = os.name == "nt"

#: POSIX: every primitive the pinned directory uses exists fd-relative. Read
#: ONCE at import (a test that wraps ``os.rename`` must not flip it); a test
#: forces the fail-closed branch by setting it False.
_DIR_FD_OK = (
    os.name != "nt"
    and all(f in os.supports_dir_fd for f in (os.open, os.stat, os.rename, os.link, os.unlink))
    and os.link in os.supports_follow_symlinks
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)

#: Refused reservations left on disk (see "A REFUSAL NEVER DELETES A
#: RESERVATION"). At this many empty ``<file>.aidocs-bak-*`` files next to a
#: config, the migration refuses instead of adding another: the pile is bounded.
_MAX_STRANDED_RESERVATIONS = 8


class _Dir:
    """The verified project directory, PINNED for the whole swap; every name
    the swap touches is resolved in THIS directory, never through a path that
    may since have been rebound (see "PINNED" above)."""

    def __init__(self, d: Path) -> None:
        self.d = d

    def path(self, name: str) -> Path:
        return self.d / name

    @property
    def refuses_existing(self) -> bool:  # does rename refuse an existing destination?
        return False

    def close(self) -> None:
        raise NotImplementedError

    def lstat(self, name: str) -> os.stat_result:
        raise NotImplementedError

    def open_read(self, name: str) -> int:
        raise NotImplementedError

    def create_excl(self, name: str, mode: int) -> int:
        raise NotImplementedError

    def rename(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def link(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def unlink(self, name: str) -> None:
        raise NotImplementedError

    def listdir(self) -> list[str]:
        raise NotImplementedError


class _FdDir(_Dir):
    """POSIX: a directory fd (O_DIRECTORY|O_NOFOLLOW) whose identity is the
    verified root's. Every operation is ``*at()`` relative to it, so renaming
    the directory away and planting a link at its path redirects nothing."""

    def __init__(self, d: Path, root_ident: tuple) -> None:
        super().__init__(d)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        self.fd: int | None = os.open(d, flags)
        st = os.fstat(self.fd)
        # The fd holds the inode, so "the fd's directory IS the verified root"
        # is proved once, here: same identity as the root, and the path still
        # reaches that identity link-free AFTER the fd exists. A later rename
        # or link changes what the path names, never what the fd names.
        if (st.st_dev, st.st_ino) != root_ident or not _dir_ok(d, root_ident):
            self.close()
            raise _Refused("refused_link", f"{d} is no longer the verified, link-free root")

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def lstat(self, name: str) -> os.stat_result:
        return os.stat(name, dir_fd=self.fd, follow_symlinks=False)

    def open_read(self, name: str) -> int:
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=self.fd)

    def create_excl(self, name: str, mode: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        return os.open(name, flags, mode, dir_fd=self.fd)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def link(self, src: str, dst: str) -> None:
        os.link(src, dst, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)

    def unlink(self, name: str) -> None:
        os.unlink(name, dir_fd=self.fd)

    def listdir(self) -> list[str]:
        return os.listdir(self.fd)


class _HandleDir(_Dir):
    """Windows: an open handle on the directory WITHOUT FILE_SHARE_DELETE.
    While it is held NTFS refuses to rename or remove the directory (sharing
    violation) or any ancestor (access denied: a handle is open beneath it), so
    no junction can be swapped onto the path; the path is re-verified after
    the handle is taken. Operations inside the directory are unaffected."""

    _GENERIC_READ = 0x80000000
    _SHARE_READ_WRITE = 0x1 | 0x2  # deliberately NOT FILE_SHARE_DELETE (0x4)
    _OPEN_EXISTING = 3
    _FLAGS = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS (a directory) | OPEN_REPARSE_POINT

    def __init__(self, d: Path, root_ident: tuple) -> None:
        import _winapi

        super().__init__(d)
        self._h: int | None = _winapi.CreateFile(
            str(d), self._GENERIC_READ, self._SHARE_READ_WRITE, 0, self._OPEN_EXISTING, self._FLAGS, 0
        )
        if not _dir_ok(d, root_ident):
            self.close()
            raise _Refused("refused_link", f"{d} is no longer the verified, link-free root")

    @property
    def refuses_existing(self) -> bool:
        return _RENAME_REFUSES_EXISTING

    def close(self) -> None:
        if self._h is not None:
            import _winapi

            _winapi.CloseHandle(self._h)
            self._h = None

    def lstat(self, name: str) -> os.stat_result:
        return os.lstat(self.d / name)

    def open_read(self, name: str) -> int:
        return os.open(self.d / name, os.O_RDONLY | getattr(os, "O_BINARY", 0))

    def create_excl(self, name: str, mode: int) -> int:
        return os.open(self.d / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)

    def rename(self, src: str, dst: str) -> None:
        os.rename(self.d / src, self.d / dst)

    def link(self, src: str, dst: str) -> None:
        os.link(self.d / src, self.d / dst)

    def unlink(self, name: str) -> None:
        os.unlink(self.d / name)

    def listdir(self) -> list[str]:
        return os.listdir(self.d)


def _pin(d: Path, root_ident: tuple) -> _Dir:
    """Pin the verified root, or raise _Refused. Never falls back to path ops."""
    if _nt():
        try:
            return _HandleDir(d, root_ident)
        except OSError as exc:
            raise _Refused("refused_unpinnable", f"{d} could not be pinned for the swap: {exc}") from exc
    if not _DIR_FD_OK:
        raise _Refused(
            "refused_no_dir_fd",
            "this platform lacks fd-relative open/stat/rename/link/unlink; the swap is refused, never done by path",
        )
    try:
        return _FdDir(d, root_ident)
    except OSError as exc:
        raise _Refused("refused_link", f"{d} could not be pinned (a link?): {exc}") from exc


def _holds(dr: _Dir, name: str, data: bytes, inode: tuple) -> bool:
    """``name`` in ``dr`` is a regular non-link file, inode ``inode``, holding exactly ``data``."""
    try:
        st = dr.lstat(name)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != inode:
            return False
        if getattr(st, "st_file_attributes", 0) & 0x400:  # a Windows reparse point
            return False
        fd = dr.open_read(name)
        try:
            fst = os.fstat(fd)
            if (fst.st_dev, fst.st_ino) != inode:
                return False
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
        finally:
            os.close(fd)
        return b"".join(chunks) == data
    except OSError:
        return False


def _unchanged(dr: _Dir, name: str, raw: bytes, ident: tuple) -> bool:
    try:
        return _ident(dr.lstat(name)) == ident and _holds(dr, name, raw, ident[:2])
    except OSError:
        return False


def _exists(dr: _Dir, name: str) -> bool:
    try:
        dr.lstat(name)
        return True
    except OSError:
        return False


def _install_noreplace(dr: _Dir, src: str, dst: str) -> None:
    """Move ``src`` to ``dst`` only if ``dst`` does not exist (raises otherwise)."""
    if dr.refuses_existing:
        dr.rename(src, dst)
    else:
        dr.link(src, dst)  # EEXIST if dst exists
        dr.unlink(src)


def _reserve(dr: _Dir, stem: str) -> tuple[str, tuple]:
    """Atomically RESERVE a fresh name: an empty sentinel created with
    O_CREAT|O_EXCL, so a concurrent sweep that wanted the same name fails to
    create it and takes the next one (r0b2 on 68c75524c). Returns the name
    and the sentinel's (dev, ino) -- the identity that proves it is ours."""
    name, n = stem, 0
    while True:
        try:
            fd = dr.create_excl(name, 0o600)
            break
        except FileExistsError:
            n += 1
            name = f"{stem}-{n}"
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    return name, (st.st_dev, st.st_ino)


def _move_onto_reservation(dr: _Dir, src: str, dst: str, inode: tuple) -> None:
    """Move ``src`` onto the name ``dst`` this invocation reserved. A rename
    never lands on anything but our own verified sentinel: on Windows the
    sentinel is removed and the rename itself refuses any file that took the
    name meanwhile; on POSIX the rename replaces the sentinel just verified
    (by identity and emptiness) to be ours."""
    if not _holds(dr, dst, b"", inode):
        raise FileExistsError(f"the reserved name {dst} is no longer this sweep's sentinel")
    if dr.refuses_existing:
        dr.unlink(dst)
        dr.rename(src, dst)
    else:
        dr.rename(src, dst)


def _put_back(dr: _Dir, src: str, dst: str) -> bool:
    try:
        _install_noreplace(dr, src, dst)
        return True
    except OSError:
        return False


def _drop(dr: _Dir, name: str) -> None:
    try:
        dr.unlink(name)
    except OSError:
        pass


def _stranded_reservations(dr: _Dir, filename: str) -> int:
    """Empty ``<filename>.aidocs-bak-*`` files: reservations earlier refusals left."""
    n = 0
    for entry in dr.listdir():
        if not entry.startswith(f"{filename}.aidocs-bak-"):
            continue
        try:
            st = dr.lstat(entry)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size == 0:
            n += 1
    return n


def _create_temp(dr: _Dir, filename: str, data: bytes) -> str:
    """The new content, fsynced, under a fresh O_EXCL name in the pinned directory."""
    try:
        mode = stat.S_IMODE(dr.lstat(filename).st_mode)
    except OSError:
        mode = 0o600
    for _ in range(64):
        name = f".{filename}.aidocs-mig-{secrets.token_hex(6)}"
        try:
            fd = dr.create_excl(name, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                try:
                    os.fchmod(fh.fileno(), mode)
                except (OSError, AttributeError, NotImplementedError):
                    pass
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            _drop(dr, name)
            raise
        return name
    raise _Refused("refused_io_error", "no free temp name in the project directory")


class _Swap:
    """A completed swap, undoable while the live file is still exactly ours.
    Resolves every name in the pinned directory ``dr``, which the caller
    (``_migrate_host``) holds open until after the SQL settle and any undo."""

    def __init__(self, dr: _Dir, name: str, backup_name: str, ours: bytes, ours_inode: tuple) -> None:
        self.dr = dr
        self.name = name
        self.backup_name = backup_name
        self.path = dr.path(name)
        self.backup = dr.path(backup_name)
        self.ours = ours
        self.ours_inode = ours_inode

    def undo(self) -> bool:
        """Put the moved-aside file back live. True when it is live again; False
        leaves whatever is live (a later edit is never removed) and the backup."""
        aside, a_ino = _reserve(self.dr, f".{self.name}.aidocs-undo")
        try:
            _move_onto_reservation(self.dr, self.name, aside, a_ino)
        except OSError:
            return False  # the empty reservation stays: a refusal never deletes one
        if not _holds(self.dr, aside, self.ours, self.ours_inode):
            _put_back(self.dr, aside, self.name)  # not ours any more: it stays live
            return False
        _drop(self.dr, aside)
        return _put_back(self.dr, self.backup_name, self.name)


# ── the SQL authority, bound to the SAME pinned project ──────────────────────────────
#
# The registry row and the file must settle in ONE verified adopted project,
# through plan, apply and any undo (review of 7ab29fd8c): the store opens
# <root>/.MEMORY/.index/aidocs.sqlite3 BY PATH, so a root or ancestor renamed
# away and replaced by a link between the file step and the SQL step would
# send the CAS / schema / ingest to a look-alike project's DB (or create one
# there) while the file was migrated in the real one.
#   POSIX: ``.MEMORY`` and ``.index`` are opened O_DIRECTORY|O_NOFOLLOW
#     relative to the pinned root fd, and the store is handed
#     ``/proc/self/fd/<root_fd>`` as its project root and
#     ``/proc/self/fd/<index_fd>`` as its index directory (``_PinnedStore``).
#     The kernel resolves those magic links to the directories the fds hold,
#     whatever was renamed or linked since; SQLite's -wal/-shm/-journal names
#     are derived from the same string, so they land beside the pinned DB. No
#     /proc (macOS, a /proc-less container): ``refused_no_proc_fd`` BEFORE any
#     write -- never a by-path fallback, and never a file-only migration that
#     would leave file and SQL disagreeing or need an undo to repair.
#   Windows: ``.MEMORY`` and ``.index`` get their own handles without
#     FILE_SHARE_DELETE, like the root. While they are held, NTFS refuses to
#     rename the root, ``.MEMORY``, ``.index`` or ANY ancestor, and no junction
#     can take an existing name on the chain (probe, 2026-10-23: rename of the
#     root/.MEMORY/.index -> winerror 32; of parent, grandparent and
#     great-grandparent -> winerror 5; CreateJunction over an existing parent
#     or grandparent path -> winerror 183; files inside .index create/delete
#     normally). The chain is re-verified link-free after the handles are
#     taken, so the by-path DB open cannot be redirected.
# The pins are taken BEFORE the plan reads the row, and held until after the
# settle and any undo, so the plan, the CAS and the rollback all see one DB.


# ── the trusted path (POSIX precondition; review of 5c3e1f6bd) ──────────────
#
# THREAT MODEL (operator-approved). Processes running as the operator's own
# OS user are OS-TRUSTED: AIDOCS agents are contained by the AIDOCS gate, not
# by file permissions. root and CAP_DAC_OVERRIDE / CAP_FOWNER / CAP_SYS_ADMIN
# are out of scope. The adversary is ANOTHER OS user, or a process outside
# AIDOCS's control running as one.
#
# WHY A PRECONDITION. Pins (fds, /proc/self/fd) do not stop SQLite re-deriving
# paths: it canonicalises /proc/self/fd/N back to the real path and opens
# -wal/-shm by that string, so after a rename plus symlink a commit on an
# already-open WAL connection reaches the look-alike's WAL/SHM (shown on
# Linux by the reviewer). The pins stay as defence in depth; what makes the
# DB path stable is that NO untrusted user can change anything on it.
#
# WHAT IS PROVEN, before ANY work (no config read, temp, mkdir or SQL): walking
# from / to <root> (and, when the registry is synced, <root>/.MEMORY/.index)
# fd-relative with O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC, every component is a real
# directory (never a symlink; the opened fd's identity equals the entry's),
# owned by the effective uid or root, NOT group- or other-writable, and
# carrying no POSIX ACL (an ACL can grant write behind the mode bits: refuse
# rather than guess; a platform that cannot list them refuses). One exception:
# a STRICT ancestor of the root that is group/other-writable is accepted when
# it is sticky (+t) AND the next component is owned by euid/root (e.g. /tmp):
# the sticky bit stops other users renaming or removing an entry they do not
# own. The root, .MEMORY and .index never get the exception. The DB and any
# existing -wal/-shm/-journal must be regular, non-symlink, single-link
# (st_nlink == 1), trusted-owned, not group/other-writable and ACL-free, with
# the identity proven at an O_NOFOLLOW open; absent ones are created only
# inside the proven .index, and every SQLite connection is checked to have
# opened the proven DB (``_PinnedStore.connect``). Any failure, a missing
# component included, is ``refused_untrusted_path``. Nothing is ever chmod'ed,
# chown'ed or cleaned up to make a project pass: the operator decides.
#
# WHAT IS NOT CLAIMED. A SAME-UID actor CAN STILL RENAME any of these
# directories, even under 0700 -- it is in the trusted set by definition, and
# this check makes no claim against it. Mounts: every component's owner is
# checked, so a mount whose root is foreign-owned is refused (an st_dev change
# alone is not); LIMIT: bind mounts / user namespaces that present a
# trusted-looking owner for a foreign tree are not detected. Windows is
# unchanged (the held-handle chain); ACL-based Windows trust is out of scope.

_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")
_DB_NAME = "aidocs.sqlite3"
_DB_SIDECARS = ("-wal", "-shm", "-journal")


class _Trust:
    """Identities the preflight proved; the pins must match them."""

    def __init__(self, root: tuple, memory: tuple | None, index: tuple | None, db: tuple | None) -> None:
        self.root = root
        self.memory = memory
        self.index = index
        self.db = db


def _untrusted(detail: str) -> _Refused:
    return _Refused("refused_untrusted_path", detail)


def _owner_uid(st: os.stat_result) -> int:
    """The owner the trust check reads (a seam: tests simulate a foreign owner)."""
    return st.st_uid


def _xattr_names(fd: int) -> list[str]:
    """Extended attribute names of an open fd (a seam: tests simulate an ACL)."""
    return list(os.listxattr(fd))


def _trusted_owner(st: os.stat_result) -> bool:
    return _owner_uid(st) in (os.geteuid(), 0)


def _refuse_acl(fd: int, what: str) -> None:
    import errno

    if not hasattr(os, "listxattr"):
        raise _untrusted(f"{what}: this platform cannot list POSIX ACLs; refusing rather than guessing effective permissions")
    try:
        names = _xattr_names(fd)
    except OSError as exc:
        if exc.errno in (errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)):
            return  # the filesystem has no xattrs, hence no ACLs
        raise _untrusted(f"{what}: cannot read its ACLs ({exc})") from exc
    acl = [n for n in names if n in _ACL_XATTRS]
    if acl:
        raise _untrusted(f"{what} carries a POSIX ACL ({', '.join(acl)}) that may grant write beyond its mode")


def _open_dir_at(dfd: int, name: str, what: str) -> tuple[int, os.stat_result]:
    try:
        lst = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise _untrusted(f"{what} is missing") from exc
    except OSError as exc:
        raise _untrusted(f"{what}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise _untrusted(f"{what} is a symlink")
    if not stat.S_ISDIR(lst.st_mode):
        raise _untrusted(f"{what} is not a directory")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dfd)
    except OSError as exc:
        raise _untrusted(f"{what} could not be opened without following links: {exc}") from exc
    st = os.fstat(fd)
    if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino):
        os.close(fd)
        raise _untrusted(f"{what} changed while it was opened")
    return fd, st


def _check_dir(fd: int, st: os.stat_result, what: str, child: os.stat_result | None) -> None:
    """``child`` is the next component's stat when this is a strict ancestor
    of the root (the only place the sticky exception applies), else None."""
    if not _trusted_owner(st):
        raise _untrusted(f"{what} is owned by uid {_owner_uid(st)}, not this user or root")
    _refuse_acl(fd, what)
    if st.st_mode & 0o022:
        if child is not None and st.st_mode & stat.S_ISVTX and _trusted_owner(child):
            return
        raise _untrusted(f"{what} is group/other-writable (mode {stat.S_IMODE(st.st_mode):o})")


def _check_file_at(dfd: int, name: str, what: str) -> tuple | None:
    """A trusted regular file's (dev, ino), or None when absent."""
    try:
        lst = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _untrusted(f"{what}: {exc}") from exc
    if stat.S_ISLNK(lst.st_mode):
        raise _untrusted(f"{what} is a symlink")
    if not stat.S_ISREG(lst.st_mode):
        raise _untrusted(f"{what} is not a regular file")
    if lst.st_nlink != 1:
        raise _untrusted(f"{what} has {lst.st_nlink} hard links")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=dfd)
    except OSError as exc:
        raise _untrusted(f"{what} could not be opened without following links: {exc}") from exc
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (lst.st_dev, lst.st_ino) or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise _untrusted(f"{what} changed while it was opened")
        if not _trusted_owner(st):
            raise _untrusted(f"{what} is owned by uid {_owner_uid(st)}, not this user or root")
        if st.st_mode & 0o022:
            raise _untrusted(f"{what} is group/other-writable (mode {stat.S_IMODE(st.st_mode):o})")
        _refuse_acl(fd, what)
        return (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def _trusted_preflight(root: Path, *, registry: bool) -> _Trust | None:
    """POSIX: prove the trusted path (see above) or raise refused_untrusted_path.
    Windows: None (the held-handle chain applies instead). Reads only."""
    if _nt():
        return None
    if not hasattr(os, "geteuid"):
        raise _untrusted("no effective uid on this platform")
    parts = Path(os.path.normpath(str(root))).parts
    if not parts or parts[0] != "/":
        raise _untrusted(f"{root} is not an absolute POSIX path")
    names = list(parts[1:])
    root_at = len(names)  # names[root_at - 1] is the root itself
    if root_at == 0:
        raise _untrusted("/ is never a project root")
    if registry:
        names += [".MEMORY", ".index"]
    try:
        cur = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise _untrusted(f"/ could not be opened: {exc}") from exc
    cur_st, cur_what = os.fstat(cur), "/"
    ids: list[tuple] = []
    try:
        for i, name in enumerate(names):
            what = "/" + "/".join(names[: i + 1])
            fd, st = _open_dir_at(cur, name, what)
            try:
                _check_dir(cur, cur_st, cur_what, st if i < root_at else None)
            except BaseException:
                os.close(fd)
                raise
            os.close(cur)
            cur, cur_st, cur_what = fd, st, what
            ids.append((st.st_dev, st.st_ino))
        _check_dir(cur, cur_st, cur_what, None)
        db = None
        if registry:
            db = _check_file_at(cur, _DB_NAME, f"{cur_what}/{_DB_NAME}")
            for suffix in _DB_SIDECARS:
                _check_file_at(cur, _DB_NAME + suffix, f"{cur_what}/{_DB_NAME}{suffix}")
    finally:
        os.close(cur)
    if registry:
        return _Trust(ids[root_at - 1], ids[root_at], ids[root_at + 1], db)
    return _Trust(ids[root_at - 1], None, None, None)


def _in_gate_host() -> bool:
    """Belt and braces: the writing sweep never runs inside a gate dispatch."""
    from .mcp_server_runtime_helpers import in_gate_dispatch

    return bool(in_gate_dispatch())


def _proc_fd_path(fd: int) -> str:
    """``/proc/self/fd/<fd>``, proved to name the very directory ``fd`` holds."""
    p = f"/proc/self/fd/{fd}"
    try:
        if os.path.samestat(os.stat(p), os.fstat(fd)):
            return p
    except OSError:
        pass
    raise _Refused(
        "refused_no_proc_fd",
        "the registry cannot be opened through the pinned directory (no /proc/self/fd here); "
        "the migration is refused rather than updating SQL by path",
    )


def _pinned_store_class():
    import sqlite3

    from .mcp_registry_store import McpRegistryStore

    class _PinnedStore(McpRegistryStore):
        """The registry store with its index directory fixed to the pinned one.
        POSIX: every connection is checked, before it is used, to have opened
        the preflighted DB (or, when there was none, a regular single-link
        file it created inside the proven .index) -- the file SQLite actually
        opened, as it reports it in ``PRAGMA database_list``."""

        def __init__(self, index_dir: Path, index_ident: tuple | None = None, db_ident: tuple | None = None) -> None:
            self._pinned_index = index_dir
            self._index_ident = index_ident
            self._db_ident = db_ident

        def index_root(self, project_root: Path) -> Path:
            return self._pinned_index

        def connect(self, project_root: Path):
            conn = super().connect(project_root)
            if self._index_ident is None:
                return conn
            try:
                opened = ""
                for row in conn.execute("PRAGMA database_list").fetchall():
                    if row[1] == "main":
                        opened = str(row[2] or "")
                st = os.stat(opened, follow_symlinks=False)
                parent = os.stat(os.path.dirname(opened), follow_symlinks=False)
                ident = (st.st_dev, st.st_ino)
                ok = (
                    stat.S_ISREG(st.st_mode)
                    and st.st_nlink == 1
                    and (parent.st_dev, parent.st_ino) == self._index_ident
                    and (self._db_ident is None or ident == self._db_ident)
                )
                if not ok:
                    raise sqlite3.DatabaseError(f"the registry connection opened {opened!r}, not the preflighted DB")
                self._db_ident = ident
            except BaseException:
                conn.close()
                raise
            return conn

    return _PinnedStore


class _PinnedRegistry:
    """``root`` (the project-root argument for every store call) and ``store``,
    both resolving into the pinned project only. ``close()`` releases the
    .MEMORY / .index pins (the root pin belongs to the caller)."""

    def __init__(self, dr: _Dir, real_root: Path, trust: _Trust | None = None) -> None:
        self._closers: list = []
        try:
            if isinstance(dr, _FdDir):
                self._open_posix(dr, trust)
            else:
                self._open_windows(dr, real_root)
        except BaseException:
            self.close()
            raise

    def _open_posix(self, dr: _FdDir, trust: _Trust | None) -> None:
        if dr.fd is None:
            raise _Refused("error_sql_failed", "the project pin is already closed")
        if trust is None or trust.memory is None or trust.index is None:
            raise _untrusted("the registry was not covered by the trusted-path preflight")
        from . import _sqlite_index_store_base as _base

        # Inside connection_scope() idle handles are pooled by the db path
        # STRING; ``/proc/self/fd/<n>`` strings repeat as fd numbers are reused,
        # so a pooled handle could belong to another project. Refuse there.
        if int(getattr(_base._scope_state, "depth", 0) or 0) > 0:  # noqa: SLF001
            raise _Refused("error_sql_failed", "the pinned registry cannot be used inside a connection_scope()")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        fds: list[int] = []
        for name, want in ((".MEMORY", trust.memory), (".index", trust.index)):
            try:
                fd = os.open(name, flags, dir_fd=fds[-1] if fds else dr.fd)
            except OSError as exc:
                # The preflight proved both exist; neither is ever created here.
                raise _untrusted(f"{name} in the pinned project could not be opened without following links: {exc}") from exc
            self._closers.append(lambda fd=fd: os.close(fd))
            st = os.fstat(fd)
            if (st.st_dev, st.st_ino) != want:
                raise _untrusted(f"{name} in the pinned project is not the directory the preflight proved")
            fds.append(fd)
        self.root = Path(_proc_fd_path(dr.fd))
        self.store = _pinned_store_class()(Path(_proc_fd_path(fds[1])), trust.index, trust.db)

    def _open_windows(self, dr: _Dir, real_root: Path) -> None:
        mem_p = real_root / ".MEMORY"
        idx_p = mem_p / ".index"
        mem_ident = _pin_child(self, mem_p)
        if not os.path.lexists(idx_p):
            try:
                idx_p.mkdir()
            except FileExistsError:
                pass
            except OSError as exc:
                raise _Refused("error_sql_failed", f"cannot create .MEMORY/.index: {exc}") from exc
        _pin_child(self, idx_p)
        # Every directory on the chain is now unmovable; prove it is the
        # verified, link-free chain AFTER the handles exist.
        if not (_no_links(idx_p) and _dir_ok(mem_p, mem_ident)):
            raise _Refused("refused_link", f"{idx_p} is not a link-free path inside the pinned project")
        self.root = real_root
        self.store = _pinned_store_class()(idx_p)

    def close(self) -> None:
        while self._closers:
            try:
                self._closers.pop()()
            except OSError:
                pass


def _pin_child(reg: _PinnedRegistry, p: Path) -> tuple:
    """Windows: hold a no-delete-sharing handle on directory ``p`` (owned by
    ``reg``); refuse a reparse point. Returns its (dev, ino)."""
    import _winapi

    try:
        h = _winapi.CreateFile(
            str(p), _HandleDir._GENERIC_READ, _HandleDir._SHARE_READ_WRITE, 0,
            _HandleDir._OPEN_EXISTING, _HandleDir._FLAGS, 0,
        )
    except OSError as exc:
        raise _Refused("refused_unpinnable", f"{p} could not be pinned: {exc}") from exc
    reg._closers.append(lambda: _winapi.CloseHandle(h))
    if _is_link(p):
        raise _Refused("refused_link", f"{p} is a junction or reparse point")
    st = os.lstat(p)
    if not stat.S_ISDIR(st.st_mode):
        raise _Refused("refused_not_a_directory", str(p))
    return (st.st_dev, st.st_ino)


def _cas_replace(dr: _Dir, path: Path, raw: bytes, ident: tuple, text: str, root_ident: tuple, pre_swap) -> _Swap:
    """In the pinned directory ``dr``: move aside, verify, install no-replace,
    re-verify (see above). Returns the swap or raises _Refused after leaving
    the user's file live. The caller owns ``dr``."""
    d, name = path.parent, path.name
    ours = text.encode("utf-8")
    tmp: str | None = _create_temp(dr, name, ours)
    try:
        if pre_swap is not None:
            pre_swap(path)
        if not _dir_ok(d, root_ident):
            raise _Refused("refused_link", f"{d} changed or became a link before the swap")
        if not _unchanged(dr, name, raw, ident):
            raise _Refused("skipped_concurrent_edit", "the file changed before the swap")
        if _stranded_reservations(dr, name) >= _MAX_STRANDED_RESERVATIONS:
            raise _Refused(
                "refused_stranded_reservations",
                f"{_MAX_STRANDED_RESERVATIONS}+ empty {name}.aidocs-bak-* reservations from earlier refusals "
                "are next to the file; remove them after checking they are empty",
            )
        backup, b_ino = _reserve(dr, f"{name}.aidocs-bak-{time.strftime('%Y%m%dT%H%M%S')}")
        try:
            _move_onto_reservation(dr, name, backup, b_ino)  # 1. move aside, onto OUR name only
        except OSError as exc:
            # The reservation is NOT released (see "A REFUSAL NEVER DELETES A
            # RESERVATION" above): an empty sentinel may stay.
            raise _Refused("skipped_concurrent_edit", f"the file could not be moved aside (in use?): {exc}") from exc
        if not (_dir_ok(d, root_ident) and _holds(dr, backup, raw, ident[:2])):  # 2. verify
            linked = not _dir_ok(d, root_ident)
            back = _put_back(dr, backup, name)
            where = "put back" if back else f"kept at {dr.path(backup)}"
            if linked:
                raise _Refused("refused_link", f"{d} changed or became a link during the swap; the file was {where}")
            raise _Refused("skipped_concurrent_edit", f"the file changed in the last window; the user's file was {where}")
        try:
            _install_noreplace(dr, tmp, name)  # 3. install, never over anything
            tmp = None  # installed: the name is no longer ours to clean up
        except OSError as exc:
            back = not _exists(dr, name) and _put_back(dr, backup, name)
            where = "put back" if back else f"kept at {dr.path(backup)}"
            raise _Refused("skipped_concurrent_edit", f"a file appeared at {name} during the swap ({exc}); the original was {where}") from exc
        try:
            st = dr.lstat(name)
        except OSError as exc:
            raise _Refused("skipped_concurrent_edit", f"the new file vanished after the swap: {exc}; the original is kept at {dr.path(backup)}") from exc
        swap = _Swap(dr, name, backup, ours, (st.st_dev, st.st_ino))
        dir_ok = _dir_ok(d, root_ident)
        if dir_ok and _holds(dr, backup, raw, ident[:2]):  # 4. re-verify
            return swap
        where = "restored" if swap.undo() else f"kept at {dr.path(backup)}"
        if not dir_ok:
            raise _Refused("refused_link", f"{d} changed or became a link during the swap; the original was {where}")
        raise _Refused("skipped_concurrent_edit", f"the file was written during the swap; the user's file was {where}")
    finally:
        if tmp is not None:
            _drop(dr, tmp)  # by name, in the PINNED directory only


# ── the SQL authority row (r0b2 block 2) ─────────────────────────────────────
#
# .mcp.json is a projection of the project's mcp_servers table, so the file and
# the row must settle together. Before anything is touched, the live row must
# be the AIDOCS-written legacy entry this file carries (or already the target);
# otherwise neither is changed (skipped_conflict). After the file swap the row
# is moved with a compare-and-set; if that fails or loses, the file swap is
# undone, so file and SQL agree again and a later sweep simply replays.

_OWNED_SOURCES = ("aidocs", "imported")


def _row_is(cols: dict, entry: dict) -> bool:
    try:
        args = json.loads(cols["args_json"])
        env = json.loads(cols["env_json"] or "{}")
    except (TypeError, ValueError):
        return False
    return (
        cols["type"] == "stdio"
        and cols["command"] == entry.get("command")
        and args == list(entry.get("args") or [])
        and env == (entry.get("env") or {})
    )


def _sql_plan(reg: _PinnedRegistry, old: dict, new: dict) -> dict:
    """Read-only ownership check, in the pinned project's DB. Raise _Refused
    when the row is not ours."""
    try:
        store, root = reg.store, reg.root
        store.ensure_schema(root)
        with store.session(root) as conn:
            ingested = conn.execute("SELECT 1 FROM mcp_registry_meta WHERE k = 'ingested'").fetchone() is not None
            r = conn.execute(
                "SELECT type, command, args_json, env_json, source FROM mcp_servers WHERE name = 'aidocs'"
            ).fetchone()
    except Exception as exc:  # noqa: BLE001 -- reported; nothing touched
        raise _Refused("error_sql_failed", f"the SQL registry is unreadable: {type(exc).__name__}: {exc}") from exc
    if r is None:
        if not ingested:
            return {"mode": "ingest"}  # pre-registry project: the file is the only record
        raise _Refused("skipped_conflict", "the SQL registry has no aidocs row (removed by the operator?); file and SQL left alone")
    cols = {"type": r[0], "command": r[1], "args_json": r[2], "env_json": r[3], "source": r[4]}
    if _row_is(cols, new):
        return {"mode": "noop"}
    if cols["source"] in _OWNED_SOURCES and _row_is(cols, old):
        return {"mode": "cas", "expect": cols}
    raise _Refused(
        "skipped_conflict",
        "the SQL aidocs row is not the AIDOCS-written legacy entry this file carries "
        f"(source={cols['source']!r}; operator-edited or newer); file and SQL left alone",
    )


def _sql_apply(reg: _PinnedRegistry, new: dict, plan: dict) -> bool:
    """Move the row, in the pinned project's DB. True when it now names ``new``;
    False when it lost a race."""
    store, root = reg.store, reg.root
    if plan["mode"] == "noop":
        return True
    if plan["mode"] == "ingest":
        store.migrate_legacy_once(root)  # imports the migrated (pinned) file, once
        with store.session(root) as conn:
            r = conn.execute(
                "SELECT type, command, args_json, env_json, source FROM mcp_servers WHERE name = 'aidocs'"
            ).fetchone()
        return r is not None and _row_is(
            {"type": r[0], "command": r[1], "args_json": r[2], "env_json": r[3], "source": r[4]}, new
        )
    return store.compare_and_set(
        root,
        "aidocs",
        expect=plan["expect"],
        command=str(new["command"]),
        args=[str(a) for a in new.get("args") or []],
        transport="stdio",
        source="aidocs",
        env=new.get("env") or {},
    )


def _settle_sql(row: dict, swap: _Swap, reg: _PinnedRegistry, new: dict, plan: dict) -> None:
    detail = ""
    try:
        moved = _sql_apply(reg, new, plan)
    except Exception as exc:  # noqa: BLE001 -- compensated below, reported
        moved = None
        detail = f"{type(exc).__name__}: {exc}"
    if moved:
        row["registry"] = "synced"
        return
    restored = swap.undo()
    if restored:
        row.pop("backup", None)
    if moved is False:
        row["status"] = "skipped_conflict" if restored else "rewritten_sql_conflict"
        detail = "the SQL aidocs row changed during the migration"
    else:
        row["status"] = "error_sql_failed" if restored else "rewritten_sql_failed"
    row["registry"] = "not_synced"
    row["detail"] = detail + ("; the file was restored" if restored else "; the file could NOT be restored -- file and SQL disagree")


def _migrate_host(
    host: _Host, real_root: Path, home: Path, target_fn: Target, *, write: bool, sync_registry: bool, pre_swap,
    trust: _Trust | None = None,
) -> dict:
    path = real_root / host.filename
    row: dict = {"host": host.name, "path": str(path)}
    try:
        got = _read_file(path, real_root)
        if got is None:
            row["status"] = "absent"
            return row
        raw, ident = got
        data = _parse(raw)
        servers = data.get(host.container)
        if not isinstance(servers, dict) or "aidocs" not in servers:
            row["status"] = "no_aidocs_entry"
            return row
        old = servers["aidocs"]
        try:
            target = target_fn(real_root)
        except Exception as exc:  # noqa: BLE001
            target = None
            row["detail"] = f"{type(exc).__name__}: {exc}"
        if not isinstance(target, dict) or not _stable_argv(host.argv_of(target), home):
            target = None
        if target is not None and isinstance(old, dict) and _merge(old, target, host) == old:
            row["status"] = "current"
            return row
        if not host.legacy(old, real_root, home):
            if isinstance(old, dict) and _stable_argv(host.argv_of(old), home):
                row["status"] = "current"  # already a stable form that cannot go stale
            else:
                row["status"] = "skipped_unknown_shape"
            return row
        if target is None:
            row["status"] = "refused_no_target"
            row.setdefault("detail", "no static shim / current launcher to point the entry at; run `aidocs runtime --fix`")
            return row
        new_entry = _merge(old, target, host)
        if not write:
            row["status"] = "stale"
            return row
        servers["aidocs"] = new_entry
        data[host.container] = servers
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if trust is not None:
            root_ident = trust.root  # POSIX: the identity the trusted-path preflight proved
        elif not _nt():
            raise _untrusted("no trusted-path preflight ran for this write")
        else:
            dst = os.lstat(real_root)
            root_ident = (dst.st_dev, dst.st_ino)
        # ONE pinned project for the whole write: the plan's row read, the file
        # swap, the SQL settle and any undo (see "the SQL authority, bound to
        # the SAME pinned project"). Released only after all of them.
        dr = _pin(real_root, root_ident)
        reg: _PinnedRegistry | None = None
        try:
            plan = None
            if sync_registry and host is _CLAUDE:
                reg = _PinnedRegistry(dr, real_root, trust)
                plan = _sql_plan(reg, old, new_entry)
            swap = _cas_replace(dr, path, raw, ident, text, root_ident, pre_swap)
            row.update(status="rewritten", backup=str(swap.backup))
            if plan is not None and reg is not None:
                _settle_sql(row, swap, reg, new_entry, plan)
        finally:
            if reg is not None:
                reg.close()
            dr.close()
        return row
    except _Refused as r:
        row["status"] = r.status
        if r.detail:
            row["detail"] = r.detail
        return row
    except OSError as exc:
        row.update(status="refused_io_error", detail=str(exc))
        return row


# ── production targets ────────────────────────────────────────────────────────


def _default_claude_target(root: Path) -> dict:
    from .runtime_project_support_service import claude_aidocs_target

    res = claude_aidocs_target(root)
    if "entry" not in res:
        raise RuntimeError(str(res.get("reason") or "no target"))
    return res["entry"]


def _default_opencode_target(home: Path) -> Target:
    from .opencode_mcp_config import opencode_mcp_entry

    return lambda root: opencode_mcp_entry(root, "", home=home)


# ── public API ────────────────────────────────────────────────────────────────


def migrate_project(
    project_root: Path | str,
    *,
    home: Path | str | None = None,
    write: bool = True,
    claude_target: Target | None = None,
    opencode_target: Target | None = None,
    sync_registry: bool = True,
    _pre_swap: Callable[[Path], None] | None = None,
) -> dict:
    """Check (``write=False``) or migrate one project's aidocs host entries."""
    h = Path(home) if home else Path.home()
    root = Path(project_root)
    out: dict = {"project": str(project_root)}
    try:
        real = _check_root(root, h)
    except _Refused as r:
        out.update(status=r.status, detail=r.detail)
        return out
    except OSError as exc:
        out.update(status="refused_io_error", detail=str(exc))
        return out
    trust: _Trust | None = None
    if write:
        # Belt and braces: never inside a gate dispatch (the outer gate's
        # per-request flag; there is no process-wide "this is the gate" flag).
        try:
            gate = _in_gate_host()
        except Exception as exc:  # noqa: BLE001 -- cannot tell: refuse
            out.update(status="refused_gate_host", detail=f"cannot tell whether this is a gate dispatch: {exc}")
            return out
        if gate:
            out.update(status="refused_gate_host", detail="the writing sweep never runs inside a gate dispatch")
            return out
        # BEFORE ANY WORK: the trusted-path precondition (POSIX; see above).
        try:
            trust = _trusted_preflight(real, registry=sync_registry)
        except _Refused as r:
            out.update(status=r.status, detail=r.detail)
            return out
        except OSError as exc:
            out.update(status="refused_untrusted_path", detail=str(exc))
            return out
    out["status"] = "checked"
    kw = {"write": write, "sync_registry": sync_registry, "pre_swap": _pre_swap, "trust": trust}
    out["claude"] = _migrate_host(_CLAUDE, real, h, claude_target or _default_claude_target, **kw)
    out["opencode"] = _migrate_host(_OPENCODE, real, h, opencode_target or _default_opencode_target(h), **kw)
    return out


def _registry_roots(home: Path | None) -> list[str]:
    from .known_projects_store import KnownProjectsStore

    store = KnownProjectsStore()
    # The store's own override (AIDOCS_GLOBAL_CONFIG_DB) wins, exactly as for
    # every other reader; otherwise THIS home's registry -- a sweep asked about
    # one home never reads (or writes the projects of) another.
    if home is not None and not os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip():
        db = home / ".aidocs" / "config.sqlite3"
        store._global_db_path = lambda: db  # type: ignore[method-assign]
    return [str(r.get("project_root") or "") for r in store.list_projects()]


def sweep_project_host_configs(
    *,
    home: Path | str | None = None,
    write: bool,
    projects: list[str] | None = None,
    max_projects: int = DEFAULT_MAX_PROJECTS,
    time_budget_s: float = DEFAULT_TIME_BUDGET_S,
    claude_target: Target | None = None,
    opencode_target: Target | None = None,
    sync_registry: bool = True,
) -> dict:
    """The bounded sweep over registered projects. Never raises."""
    h = Path(home) if home else None
    rep: dict = {"mode": "write" if write else "check", "projects": [], "counts": {}, "truncated": False, "budget_exhausted": False}
    try:
        roots = projects if projects is not None else _registry_roots(h)
    except Exception as exc:  # noqa: BLE001
        rep.update(ok=False, error=f"registry unreadable: {type(exc).__name__}: {exc}")
        return rep
    seen: set[str] = set()
    uniq = [r for r in roots if r and not (_norm(r) in seen or seen.add(_norm(r)))]
    deadline = time.monotonic() + max(0.0, float(time_budget_s))
    for i, r in enumerate(uniq):
        if i >= max_projects:
            rep["truncated"] = True
            break
        if time.monotonic() > deadline:
            rep["budget_exhausted"] = rep["truncated"] = True
            break
        try:
            row = migrate_project(
                r, home=h, write=write, claude_target=claude_target,
                opencode_target=opencode_target, sync_registry=sync_registry,
            )
        except Exception as exc:  # noqa: BLE001
            row = {"project": r, "status": "refused_internal_error", "detail": f"{type(exc).__name__}: {exc}"}
        rep["projects"].append(row)
    rep["remaining"] = max(0, len(uniq) - len(rep["projects"]))
    counts: dict[str, int] = {}
    for row in rep["projects"]:
        sts = [row["status"]] if row["status"] != "checked" else [row["claude"]["status"], row["opencode"]["status"]]
        for s in sts:
            counts[s] = counts.get(s, 0) + 1
    rep["counts"] = counts
    healthy = not any(_needs_attention(s) for s in counts)
    # r0b2 block 3: a sweep that did not reach every project is PARTIAL, never ok.
    if rep["truncated"] or rep["budget_exhausted"] or rep["remaining"]:
        rep.update(ok=False, status="partial")
    else:
        rep.update(ok=healthy, status="ok" if healthy else "attention")
    return rep


def _needs_attention(status: str) -> bool:
    """A per-host status that makes the sweep NOT ok."""
    return status.startswith(("refused_", "error_", "rewritten_sql_")) or status == "stale"


def entry_point_refusal(reconcile: object) -> str:
    """Why the writing sweep must NOT run after this ``reconcile_entry_points``
    result, or "" when it may. The sweep points entries at the static shim and
    launchers that reconcile just placed; if it refused or failed, there is
    nothing verified to point at, so nothing is rewritten."""
    if not isinstance(reconcile, dict):
        return "entry-point reconcile did not run"
    if "launchers" not in reconcile or "stdio_shim" not in reconcile:
        return "entry-point reconcile failed: " + "; ".join(map(str, reconcile.get("errors") or ["no result"]))
    for part in ("launchers", "stdio_shim"):
        r = reconcile.get(part)
        if not isinstance(r, dict) or r.get("refused") or r.get("ok") is not True:
            errs = "; ".join(map(str, (r or {}).get("errors") or [])) if isinstance(r, dict) else ""
            return f"entry-point reconcile {part} refused or failed" + (f": {errs}" if errs else "")
    return ""


def skipped_sweep(reason: str) -> dict:
    """The report when the writing sweep is withheld (nothing read or written)."""
    return {"mode": "write", "ok": False, "skipped": reason, "projects": [], "counts": {}, "truncated": False}


def summary_line(rep: dict) -> str:
    if rep.get("skipped"):
        return f"{SUMMARY_PREFIX} [{rep.get('mode')}] NOT RUN: {rep['skipped']}"
    counts = ", ".join(f"{k}={v}" for k, v in sorted((rep.get("counts") or {}).items())) or "none"
    tail = f" PARTIAL (+{rep.get('remaining')} not reached)" if rep.get("status") == "partial" or rep.get("truncated") else ""
    return f"{SUMMARY_PREFIX} [{rep.get('mode')}] {counts}{tail}"


def log_report(rep: dict) -> None:
    """The reconcile log: one summary line, one line per project that needs
    attention. Never model-visible."""
    _log.info(summary_line(rep))
    for row in rep.get("projects") or []:
        sts = [row.get("status")] + [row.get(k, {}).get("status") for k in ("claude", "opencode") if isinstance(row.get(k), dict)]
        bad = [s for s in sts if s and (s.startswith("skipped_") or _needs_attention(s))]
        if bad:
            _log.warning("%s %s: %s", SUMMARY_PREFIX, row.get("project"), ", ".join(bad))


# ── user scope: ~/.claude.json (#1202, r0b2 b7b33e8e-36e) ──────────────────────
#
# READ-ONLY, BY RULING. Claude Code keeps its USER-scope MCP servers in the
# top-level ``mcpServers`` of ~/.claude.json, a host-owned file it rewrites
# constantly. Measured 2026-10-27: the operator's aidocs entry lived there as
# ``<legacy venv> -m aidocs_mcp.stdio_shim``, the sweep above never looked, and
# every /mcp reconnect respawned the old module shim. r0b2 BLOCKED silent edits
# (a whole-file rewrite changes unrelated bytes and races the host), so this
# only DETECTS the one AIDOCS-written shape and names the host-native CLI
# commands the OPERATOR runs. Nested ``projects[path].mcpServers`` entries are
# listed, never remedied.


def _user_entry_shape_ok(entry: object) -> bool:
    """The exact user-scope ENTRY shape (r0b2 B1/B2): type stdio, exactly the
    four keys, string command, an all-string args list, and env exactly the
    loopback endpoint. Types first, so nothing below can raise on odd JSON."""
    if not isinstance(entry, dict) or set(entry) != _CLAUDE_KEYS or entry.get("type") != "stdio":
        return False
    args = entry.get("args")
    if not isinstance(entry.get("command"), str) or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return False
    env = entry.get("env")
    return isinstance(env, dict) and set(env) == {"AIDOCS_MCP_ENDPOINT"} and _loopback_endpoint_ok(env["AIDOCS_MCP_ENDPOINT"])


def _user_entry_current(entry: dict, home: Path) -> bool:
    """CURRENT only for the exact shape running EXACTLY today's static shim
    argv -- never the looser launcher-prefix arm of ``_stable_argv``."""
    try:
        want = rl.static_shim_argv(home)
    except rg.CurrentGenerationError:
        return False
    return _argv_eq([entry["command"], *entry["args"]], want)


def _user_entry_remedy(entry: dict, home: Path) -> dict:
    """Operator-run repair, structured (r0b2 B3). Only the bash form is offered
    as a copy-paste command: it is the shell Claude Code's ``!`` prompt runs
    and ``shlex.quote`` is exact for it. Windows PowerShell 5.1 drops the
    embedded double quotes of a native-command JSON argument and cmd.exe does
    not group single-quoted ones, so no one-liner is claimed for either."""
    import shlex

    argv = [str(a).replace("\\", "/") for a in rl.static_shim_argv(home)]
    new = {"type": "stdio", "command": argv[0], "args": argv[1:], "env": dict(entry["env"])}
    body = json.dumps(new, separators=(",", ":"))
    # THE BACKUP IS THIS RECORD (r0b2 87114cd0-689). Claude Code resolves a
    # same-named server local > project > user with no merging, so `claude mcp
    # get` may show a SHADOWING entry; the parser already holds the exact
    # user-scope record being removed, so that is what restore re-adds.
    original = json.loads(json.dumps(entry))
    return {
        "restore": {
            "entry": original,
            "bash": f"claude mcp add-json aidocs {shlex.quote(json.dumps(original, separators=(',', ':')))} -s user",
        },
        "diagnostic": (
            "claude mcp get aidocs shows the EFFECTIVE entry; a local or project aidocs "
            "entry shadows user scope (local > project > user), so it is not a backup of this record"
        ),
        "entry": new,
        "bash": [
            "claude mcp remove aidocs -s user",
            f"claude mcp add-json aidocs {shlex.quote(body)} -s user",
        ],
        "other_shells": (
            "run the bash commands from Git Bash or Claude Code's ! prompt; no PowerShell or "
            "cmd.exe one-liner is offered, because those shells strip or misparse the JSON quotes"
        ),
        "verify": [
            "claude mcp get aidocs: Args must read -E -s -S and the static shim path",
            "restart Claude Code: a /mcp reconnect relaunches the command it loaded at startup",
            "ai_version: host_entry_fresh true and transport_fresh true",
        ],
        "note": (
            "remove then add is not atomic: if add-json fails, run restore.bash to put the exact "
            "user-scope record back"
        ),
    }


def claude_user_entry_check(home: Path | str | None = None) -> dict:
    """Is the user-scope aidocs entry the static shim? READ-ONLY; never raises.

    Statuses: ``absent`` / ``no_aidocs_entry`` / ``current`` (the exact shape
    running today's static shim argv) / ``stale`` (the exact legacy shape, with
    ``remedy``) / ``skipped_unknown_shape`` (anything else, left alone and never
    remedied) / ``refused_<reason>`` (a linked, odd or unparseable file).
    ``nested_project_entries`` lists project paths whose local-scope aidocs
    entry exists, report-only.
    """
    h = Path(home) if home else Path.home()
    path = h / ".claude.json"
    row: dict = {"host": "claude-user", "path": str(path)}
    try:
        got = _read_file(path, Path(os.path.realpath(h)))
        if got is None:
            row["status"] = "absent"
            return row
        data = _parse(got[0])
        projects = data.get("projects")
        if isinstance(projects, dict):
            nested = [
                str(p)
                for p, cfg in projects.items()
                if isinstance(cfg, dict) and isinstance(cfg.get("mcpServers"), dict) and "aidocs" in cfg["mcpServers"]
            ]
            if nested:
                row["nested_project_entries"] = nested
        servers = data.get("mcpServers")
        if not isinstance(servers, dict) or "aidocs" not in servers:
            row["status"] = "no_aidocs_entry"
            return row
        entry = servers["aidocs"]
        if not _user_entry_shape_ok(entry):
            row["status"] = "skipped_unknown_shape"
            return row
        if _user_entry_current(entry, h):
            row["status"] = "current"
            return row
        if not _claude_user_legacy(entry, h):
            row["status"] = "skipped_unknown_shape"
            return row
        row["status"] = "stale"
        try:
            row["remedy"] = _user_entry_remedy(entry, h)
        except rg.CurrentGenerationError as exc:
            row["detail"] = f"no static shim to point at yet ({exc}); run `aidocs runtime --fix` first"
        return row
    except _Refused as r:
        row["status"] = r.status
        if r.detail:
            row["detail"] = r.detail
        return row
    except OSError as exc:
        row.update(status="refused_io_error", detail=str(exc))
        return row
    except Exception as exc:  # noqa: BLE001 -- never raises: an unclassifiable entry is unknown
        row.update(status="skipped_unknown_shape", detail=f"{type(exc).__name__}: {exc}")
        row.pop("remedy", None)
        return row


def boot_doctor_check(
    *,
    home: Path | str | None = None,
    claude_target: Target | None = None,
    opencode_target: Target | None = None,
) -> dict:
    """Daemon BOOT: READ-ONLY. Reports stale / hand-edited / refused per
    project, and the user-scope ~/.claude.json entry, and writes nothing.
    Never raises."""
    try:
        rep = sweep_project_host_configs(
            home=home, write=False, claude_target=claude_target,
            opencode_target=opencode_target, sync_registry=False,
        )
        log_report(rep)
        rep["claude_user"] = claude_user_entry_check(home)
        if rep["claude_user"].get("status") not in ("absent", "no_aidocs_entry", "current"):
            _log.warning("%s user-scope ~/.claude.json aidocs entry: %s", SUMMARY_PREFIX, rep["claude_user"]["status"])
        return rep
    except Exception as exc:  # noqa: BLE001
        return {"mode": "check", "ok": False, "error": repr(exc), "projects": [], "counts": {}}
