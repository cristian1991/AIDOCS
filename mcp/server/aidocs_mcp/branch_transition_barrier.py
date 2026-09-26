"""Project-scoped BRANCH-TRANSITION BARRIER + shared MUTATION LEASES.

r0b send-backs on 7cb98b4d9 (blocker 2) and 1b60a1275 (holes 1 and 2).

A branch switch changes files under every actor working in the tree. Its
liveness and clean-tree checks are SAMPLES, so the switch must exclude every
governed mutation from before it samples until its HEAD receipt. Pretool/egress
checks cannot do that alone: a call already past them keeps running. So this is
a READER/WRITER protocol at the MUTATION BOUNDARY:

  * SHARED side -- ``mutation_lease(root, owner)``: every governed project
    mutation holds one from BEFORE its own barrier check until its write
    completes. Insert and barrier check happen in ONE ``BEGIN IMMEDIATE``
    transaction, so a lease is either visible to a later switch or refused by
    an earlier one -- never neither. Participants:
      - ``file_ops`` write entry points (create_file, edit_lines, batch_edit,
        str_replace, anchor_replace, batch_str_replace, extract_block) via
        ``@governed_project_mutation``;
      - ``file_delete_service.delete/restore``, ``file_rename_service.rename``;
      - ``shell_egress_service.execute`` / ``execute_shell`` (every governed
        subprocess whose cwd is in a git work tree; ai_git add/commit/pull/stash
        and ai_run all go through it).
  * EXCLUSIVE side -- ``holding(root, owner)``: takes the barrier row (new
    shared leases now refuse), then waits a bounded time for the existing
    shared leases on the root to DRAIN; if any remain it releases the barrier
    and raises ``MutationsInFlight`` naming them. Only then does the switch
    sample liveness and cleanliness.

KEYING: both sides key on the git work-tree root (nearest ancestor holding
``.git``), falling back to the resolved path, so an edit via project_root and a
subprocess via a subdirectory cwd meet the same key.

CRASH SAFETY (hole 1): a row records machine_id, pid and the process START TIME.
On the same machine a row is DEAD when the pid is gone or its start time differs
(pid reuse), ALIVE when both match -- an alive holder never expires, whatever the
TTL says. Only when liveness cannot be decided (another machine, start time
unreadable) does the TTL decide, and the holder renews it from a heartbeat
thread for as long as it holds the barrier.

The holder is identified by an unguessable per-acquisition TOKEN in a
``ContextVar`` of the acquiring call only; nothing in a request payload names it.

PRETOOL / ENFORCE (early, clearer refusals): ``barrier_refusal`` refuses every
tool on a barred project except an explicit READ allowlist and the
freeze-remedy/report tools. A store read error refuses mutations, not reads.

NOT COVERED: writes performed by a HOST's own native tools (Claude Code
Edit/Write) happen in the host process, outside AIDOCS code; they are refused
only at pretool and cannot hold a lease.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import os
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEFAULT_TTL_S = 180.0
MUTATION_TTL_S = 900.0
DEFAULT_DRAIN_S = 10.0

_HELD: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aidocs_branch_transition_token", default=""
)

#: The id of the in-process tool INVOCATION whose shared lease is in scope.
#: It is stamped into the lease owner string as ``invocation=<id>`` so another
#: authority (the test-retry edit witness) can match a live `mcp_tool:` /
#: `mcp_worker:` lease to the exact invocation it is asking about, instead of
#: conservatively treating every live in-process lease as possibly being it.
_INVOCATION: contextvars.ContextVar[str] = contextvars.ContextVar(
    "aidocs_tool_invocation_id", default=""
)


def current_tool_invocation_id() -> str:
    """The current in-process tool invocation id, or '' outside one."""
    return _INVOCATION.get()


def lease_invocation_id(owner: str) -> str:
    """The invocation id stamped in a lease ``owner`` string, or ''."""
    for part in str(owner or "").split():
        if part.startswith("invocation="):
            return part.split("=", 1)[1]
    return ""


class BarrierHeld(RuntimeError):
    """Another live holder owns the barrier for this project."""

    def __init__(self, holder: dict[str, Any]) -> None:
        self.holder = dict(holder)
        super().__init__(describe(holder))


class MutationsInFlight(RuntimeError):
    """Shared mutation leases did not drain after the barrier was taken."""

    def __init__(self, leases: list[dict[str, Any]]) -> None:
        self.leases = list(leases)
        names = "; ".join(
            f"{x.get('owner')} (pid={x.get('pid')})" for x in self.leases[:10]
        )
        super().__init__(
            f"{len(self.leases)} governed mutation(s) still in flight on this project: {names}"
        )


# ── store ─────────────────────────────────────────────────────────────────


def _db_path() -> Path:
    override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "config.sqlite3"


def root_key(project_root: Path | str) -> str:
    """The git work-tree root containing ``project_root`` (or the path itself)."""
    try:
        p = Path(str(project_root)).resolve()
    except Exception:  # noqa: BLE001
        p = Path(str(project_root))
    probe = p
    for cand in (probe, *probe.parents):
        try:
            if (cand / ".git").exists():
                p = cand
                break
        except Exception:  # noqa: BLE001
            break
    return os.path.normcase(str(p))


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS branch_transition_barrier (
           root_key TEXT PRIMARY KEY, token TEXT NOT NULL, owner TEXT NOT NULL,
           machine_id TEXT NOT NULL, pid INTEGER, start_time TEXT,
           acquired_at REAL NOT NULL, expires_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS project_mutation_leases (
           lease_id TEXT PRIMARY KEY, root_key TEXT NOT NULL, owner TEXT NOT NULL,
           machine_id TEXT NOT NULL, pid INTEGER, start_time TEXT,
           acquired_at REAL NOT NULL, expires_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_project_mutation_leases_root ON project_mutation_leases(root_key)",
    """CREATE TABLE IF NOT EXISTS native_host_presence (
           root_key TEXT NOT NULL, host_kind TEXT NOT NULL, host_session_id TEXT NOT NULL,
           last_seen REAL NOT NULL, server_mediated INTEGER NOT NULL DEFAULT 0,
           client_id TEXT NOT NULL DEFAULT '', auth_home TEXT NOT NULL DEFAULT '',
           PRIMARY KEY (root_key, host_kind, host_session_id, client_id))""",
)


#: CONTENTION, NOT CORRECTNESS. Every gate dispatch and every tool call takes a
#: lease here, in the SAME install-wide sqlite the gate uses for config and the
#: project registry. Re-running CREATE TABLE + the migration probe on each
#: acquire/release/heartbeat multiplied the write traffic, and a `database is
#: locked` raised out of this module surfaces to a caller as a generic
#: `edit_error` (_registry_invoke_edit) or `index_failed` (bootstrap_and_index)
#: — a spurious failure with no mention of a lease. So the schema work runs ONCE
#: per process per db, and the busy timeout is generous enough that contention
#: WAITS instead of raising. Fail-closed on a real store error is unchanged.
#: path -> the SCHEMA SIGNATURE this process last migrated/verified for it. A
#: bare path memo is not enough (r0b): a db replaced AT THE SAME PATH can carry
#: all three table NAMES with a legacy SHAPE, and the next write then fails on a
#: missing column. The signature is computed from the stored DDL, so any
#: replacement or migration necessarily changes it.
_SCHEMA_READY: dict[str, str] = {}
_SCHEMA_LOCK = threading.Lock()
_BUSY_TIMEOUT_MS = 15000
_SCHEMA_TABLES = ("branch_transition_barrier", "project_mutation_leases", "native_host_presence")


def _ddl_columns(sql: str) -> set[str]:
    """Column names declared by one CREATE TABLE statement."""
    import re as _re

    body = sql[sql.index("(") + 1 : sql.rindex(")")] if "(" in sql and ")" in sql else ""
    cols: set[str] = set()
    depth = 0
    current = ""
    for ch in body + ",":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            token = current.strip().split()
            if token and token[0].upper() not in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
                cols.add(_re.sub(r"[^A-Za-z0-9_]", "", token[0]))
            current = ""
        else:
            current += ch
    return {c for c in cols if c}


def _required_columns() -> dict[str, set[str]]:
    """What the CODE requires, derived from the DDL above — never a hand-kept
    list, so a column added to _SCHEMA cannot be forgotten here."""
    out: dict[str, set[str]] = {}
    for stmt in _SCHEMA:
        s = " ".join(stmt.split())
        if not s.upper().startswith("CREATE TABLE"):
            continue
        head = s[: s.index("(")]
        name = head.split()[-1]
        out[name] = _ddl_columns(s)
    return out


def _normalise_ddl(sql: str) -> str:
    """A CREATE TABLE statement reduced to its STRUCTURE.

    Case, whitespace, quoting (``"x"`` / `` `x` `` / ``[x]``) and ``IF NOT
    EXISTS`` are cosmetic and collapse, so a db written by a differently
    formatted (but structurally identical) statement is accepted and does NOT
    loop the migration. Everything else survives — column order, types, and the
    constraint text: PRIMARY KEY, UNIQUE, NOT NULL, DEFAULT. A db that has the
    right columns but has LOST ``branch_transition_barrier.root_key PRIMARY
    KEY`` or ``project_mutation_leases.lease_id PRIMARY KEY`` therefore fails to
    match and is migrated (r0b: prove the structure, not a column list)."""
    import re as _re

    s = str(sql or "")

    # TOKEN-AWARE quote handling (r0b). A GLOBAL quote strip would merge a
    # quoted identifier's CONTENT into the surrounding syntax, so a column
    # literally named `root_key TEXT` (SQLite allows `"root_key TEXT" INTEGER`)
    # would normalise to the same bytes as the canonical `root_key TEXT` column
    # — a semantically different table passing the structural check. Quotes are
    # therefore removed ONLY around a whole plain identifier; anything else
    # (spaces, punctuation, injected syntax) is preserved as ONE opaque token
    # that can never equal a canonical fragment.
    def _unquote(m: "_re.Match[str]") -> str:
        body = m.group(2)
        if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", body or ""):
            return body
        return "Q<" + (body or "").encode("utf-8").hex() + ">"

    s = _re.sub(r'(")((?:[^"]|"")*)(")', _unquote, s)
    s = _re.sub(r"(`)((?:[^`]|``)*)(`)", _unquote, s)
    s = _re.sub(r"(\[)([^\]]*)(\])", _unquote, s)
    s = _re.sub(r"\s+", " ", s).strip().upper()
    s = s.replace("IF NOT EXISTS ", "")
    s = _re.sub(r"\s*([(),])\s*", r"\1", s)
    return s


def _expected_ddl() -> dict[str, str]:
    """table -> normalised expected DDL, derived from _SCHEMA itself."""
    out: dict[str, str] = {}
    for stmt in _SCHEMA:
        s = " ".join(stmt.split())
        if not s.upper().startswith("CREATE TABLE"):
            continue
        out[s[: s.index("(")].split()[-1]] = _normalise_ddl(s)
    return out


def _schema_signature(rows) -> str:
    """A stable fingerprint of the three tables' STRUCTURE (normalised DDL)."""
    import hashlib

    blob = "\x00".join(f"{n}:{_normalise_ddl(str(s or ''))}" for n, s in sorted(rows))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _raw_connect():
    from ._sqlite_connect import Durability, connect

    return connect(
        _db_path(),
        durability=Durability.RUNTIME,
        timeout=float(_BUSY_TIMEOUT_MS) / 1000.0,
        busy_timeout_ms=_BUSY_TIMEOUT_MS,
    )


def _connect():
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    key = str(db)
    conn = _raw_connect()
    # ONE READ, not four writes. The old path re-ran CREATE TABLE + two PRAGMA
    # probes + COMMIT on every acquire/release/heartbeat, taking a write lock on
    # the install-wide sqlite each time. A read of sqlite_master takes none, and
    # it is EXACT: a db that was recreated (tests, a wiped install) re-runs the
    # schema instead of trusting a process-level memo that the file no longer
    # matches — the "no such table" this replaced.
    # The warm read proves SCHEMA GENERATION, not just table existence: it reads
    # the stored DDL (still ONE read, no write lock) and compares its signature
    # to what this process last verified for that path. A db replaced at the same
    # path with a legacy three-table shape has a different signature, so the
    # migration re-runs instead of the next write failing on a missing column.
    try:
        rows = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN "
                "('branch_transition_barrier', 'project_mutation_leases', 'native_host_presence')"
            ).fetchall()
        ]
        if len(rows) == len(_SCHEMA_TABLES):
            signature = _schema_signature(rows)
            if _SCHEMA_READY.get(key) == signature:
                return conn
            expected = _expected_ddl()
            actual = {n: _normalise_ddl(str(s or "")) for n, s in rows}
            if all(actual.get(t) == expected.get(t) for t in _SCHEMA_TABLES):
                _SCHEMA_READY[key] = signature
                return conn
    except Exception:  # noqa: BLE001 - fall through to the full schema pass
        pass
    with _SCHEMA_LOCK:
        try:
            # ATOMIC. The DROP+CREATE migration used to run OUTSIDE a
            # transaction, so between the DROP and the CREATE the table did not
            # exist — and this module is called concurrently (lease heartbeat
            # threads, nested leases, another root's first write). A concurrent
            # writer in that window got `no such table: branch_transition_barrier`
            # and a governed write failed closed: the order-dependent breakage
            # r0b and the retry lane hit. BEGIN IMMEDIATE holds the write lock
            # for the whole migration, and other connections keep reading the
            # pre-migration snapshot until it commits.
            conn.execute("BEGIN IMMEDIATE")
            # STRUCTURAL migration, not a column patch: any table whose stored
            # DDL does not match the expected structure (columns AND their
            # constraints) is dropped and recreated. These rows are ephemeral
            # locks and presence stamps — pid liveness and TTL recover them —
            # so recreating is cheap and leaves no half-migrated shape.
            expected = _expected_ddl()
            stored = {
                r[0]: str(r[1] or "")
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN "
                    "('branch_transition_barrier', 'project_mutation_leases', "
                    "'native_host_presence')"
                ).fetchall()
            }
            for table, want in expected.items():
                if table in stored and _normalise_ddl(stored[table]) != want:
                    conn.execute(f"DROP TABLE {table}")
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.commit()
            _SCHEMA_READY[key] = _schema_signature(
                [
                    (r[0], r[1])
                    for r in conn.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN "
                        "('branch_transition_barrier', 'project_mutation_leases', "
                        "'native_host_presence')"
                    ).fetchall()
                ]
            )
        except BaseException:
            conn.close()
            raise
    return conn


def _read_rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    import sqlite3

    if not _db_path().exists():
        return []
    conn = _raw_connect()
    try:
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc) or "no such column" in str(exc):
                return []
            raise
    finally:
        conn.close()


# ── holder liveness ───────────────────────────────────────────────────────


def process_start_time(pid: int) -> str | None:
    """An opaque, pid-reuse-proof start stamp for ``pid`` on this machine."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return None
            try:
                c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
                    return None
                return str((c.dwHighDateTime << 32) | c.dwLowDateTime)
            finally:
                k32.CloseHandle(h)
        stat = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8", errors="ignore")
        return stat.rsplit(")", 1)[1].split()[19]
    except Exception:  # noqa: BLE001
        return None


def _me() -> tuple[str, int, str | None]:
    from .host_concurrency_store import machine_id

    return machine_id(), os.getpid(), process_start_time(os.getpid())


def holder_state(row: dict[str, Any]) -> str:
    """'alive' | 'dead' | 'unknown' for the process owning ``row``."""
    try:
        from .host_concurrency_store import _is_pid_alive, machine_id

        if str(row.get("machine_id")) != machine_id() or row.get("pid") is None:
            return "unknown"
        pid = int(row["pid"])
        if not _is_pid_alive(pid):
            return "dead"
        recorded = str(row.get("start_time") or "")
        now_st = process_start_time(pid)
        if recorded and now_st is not None:
            return "alive" if now_st == recorded else "dead"
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _is_stale(row: dict[str, Any], now: float) -> bool:
    state = holder_state(row)
    if state == "dead":
        return True
    if state == "alive":
        return False
    return float(row["expires_at"]) <= now


def describe(holder: dict[str, Any]) -> str:
    return (
        f"a branch transition is in progress on this project (holder={holder.get('owner')!r}, "
        f"pid={holder.get('pid')})"
    )


# ── exclusive side: the barrier ───────────────────────────────────────────


def acquire(project_root: Path | str, owner: str, *, ttl_s: float = DEFAULT_TTL_S) -> str:
    """Atomically take the barrier; return the holder token. Raises BarrierHeld."""
    key = root_key(project_root)
    token = secrets.token_hex(16)
    mid, pid, st = _me()
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM branch_transition_barrier WHERE root_key = ?", (key,)
        ).fetchone()
        if row is not None and not _is_stale(dict(row), now):
            holder = dict(row)
            conn.rollback()
            raise BarrierHeld(holder)
        conn.execute(
            "INSERT OR REPLACE INTO branch_transition_barrier "
            "(root_key, token, owner, machine_id, pid, start_time, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, token, str(owner or "?")[:200], mid, pid, st, now, now + float(ttl_s)),
        )
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()
    return token


def renew(project_root: Path | str, token: str, *, ttl_s: float = DEFAULT_TTL_S) -> bool:
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE branch_transition_barrier SET expires_at = ? WHERE root_key = ? AND token = ?",
            (time.time() + float(ttl_s), root_key(project_root), token),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def release(project_root: Path | str, token: str) -> bool:
    """Delete the barrier row only if it still carries ``token``."""
    if not token:
        return False
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM branch_transition_barrier WHERE root_key = ? AND token = ?",
            (root_key(project_root), token),
        )
        conn.commit()
        return bool(cur.rowcount)
    finally:
        conn.close()


def active_barriers() -> list[dict[str, Any]]:
    """Every non-stale barrier row. Raises on store failure."""
    now = time.time()
    return [r for r in _read_rows("SELECT * FROM branch_transition_barrier") if not _is_stale(r, now)]


def active_barrier(project_root: Path | str) -> dict[str, Any] | None:
    key = root_key(project_root)
    for row in active_barriers():
        if row["root_key"] == key:
            return row
    return None


def live_mutation_leases(project_root: Path | str) -> list[dict[str, Any]]:
    now = time.time()
    rows = _read_rows(
        "SELECT * FROM project_mutation_leases WHERE root_key = ?", (root_key(project_root),)
    )
    return [r for r in rows if not _is_stale(r, now)]


@contextmanager
def holding(
    project_root: Path | str,
    owner: str,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    drain_timeout_s: float = DEFAULT_DRAIN_S,
    heartbeat_s: float | None = None,
) -> Iterator[str]:
    """EXCLUSIVE transition: acquire, drain shared leases, heartbeat, release.

    Raises BarrierHeld (another holder) or MutationsInFlight (leases did not
    drain within ``drain_timeout_s``; the barrier is released first)."""
    token = acquire(project_root, owner, ttl_s=ttl_s)
    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat,
        args=(project_root, token, ttl_s, heartbeat_s or max(0.05, ttl_s / 3.0), stop),
        name="aidocs-branch-transition-heartbeat",
        daemon=True,
    )
    ctx = _HELD.set(token)
    try:
        beat.start()
        deadline = time.monotonic() + max(0.0, float(drain_timeout_s))
        mine = current_tool_invocation_id()
        while True:
            # THE RULE, both halves:
            #   * a lease belonging to THIS invocation is the switch's OWN gate /
            #     tool dispatch lease. It is not a concurrent mutation, and
            #     draining against it would make ai_git switch impossible from
            #     the web surface (the access regression r0b rejected twice);
            #   * ANY OTHER invocation's lease — including another gate call's —
            #     IS a concurrent mutation and blocks, named by its invocation id.
            # Matching by invocation id is what lets the gate seam lease every
            # dispatch without the switch draining against itself.
            pending = [
                x
                for x in live_mutation_leases(project_root)
                if not (mine and lease_invocation_id(str(x.get("owner") or "")) == mine)
            ]
            if not pending:
                break
            if time.monotonic() >= deadline:
                raise MutationsInFlight(pending)
            time.sleep(0.05)
        yield token
    finally:
        stop.set()
        _HELD.reset(ctx)
        try:
            release(project_root, token)
        except Exception:  # noqa: BLE001 - liveness/TTL recovers a lost release
            pass


def _heartbeat(project_root, token, ttl_s, every_s, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            renew(project_root, token, ttl_s=ttl_s)
        except Exception:  # noqa: BLE001 - next beat retries
            pass


def _is_holder(row: dict[str, Any]) -> bool:
    tok = _HELD.get()
    return bool(tok) and secrets.compare_digest(tok, str(row.get("token") or ""))


# ── shared side: mutation leases ──────────────────────────────────────────


@contextmanager
def mutation_lease(
    project_root: Path | str,
    owner: str,
    *,
    ttl_s: float = MUTATION_TTL_S,
    heartbeat_s: float | None = None,
) -> Iterator[str]:
    """SHARED lease held across one governed mutation. Raises BarrierHeld when
    another caller holds the project's barrier (the holder itself passes).

    RENEWED while held (r0b d7137542 gap 1): a heartbeat thread pushes
    ``expires_at`` forward every ``ttl_s/3``, so a live mutation never drops out
    of a switch's drain however long it runs, even where holder liveness is
    unverifiable (other machine, unreadable start time). An ABANDONED lease
    (process gone) stops renewing and is recovered by pid liveness or TTL."""
    lease_id = secrets.token_hex(16)
    mid, pid, st = _me()
    _register_lease(project_root, lease_id, owner, mid, pid, st, ttl_s)
    stop = threading.Event()
    beat = threading.Thread(
        target=_lease_heartbeat,
        args=(lease_id, ttl_s, heartbeat_s or max(0.05, ttl_s / 3.0), stop),
        name="aidocs-mutation-lease-heartbeat",
        daemon=True,
    )
    try:
        beat.start()
        yield lease_id
    finally:
        stop.set()
        _delete_lease(lease_id)


def _register_lease(project_root, lease_id, owner, mid, pid, st, ttl_s) -> None:
    """Atomic barrier check + INSERT OR REPLACE of one shared lease row."""
    key = root_key(project_root)
    now = time.time()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM branch_transition_barrier WHERE root_key = ?", (key,)
        ).fetchone()
        if row is not None and not _is_stale(dict(row), now) and not _is_holder(dict(row)):
            holder = dict(row)
            conn.rollback()
            raise BarrierHeld(holder)
        conn.execute(
            "INSERT OR REPLACE INTO project_mutation_leases "
            "(lease_id, root_key, owner, machine_id, pid, start_time, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (lease_id, key, str(owner or "?")[:200], mid, pid, st, now, now + float(ttl_s)),
        )
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        conn.close()


def _delete_lease(lease_id: str) -> bool:
    try:
        c2 = _connect()
        try:
            cur = c2.execute("DELETE FROM project_mutation_leases WHERE lease_id = ?", (lease_id,))
            c2.commit()
            return bool(cur.rowcount)
        finally:
            c2.close()
    except Exception:  # noqa: BLE001 - pid liveness / TTL recovers
        return False


def _lease_heartbeat(lease_id: str, ttl_s: float, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            conn = _connect()
            try:
                conn.execute(
                    "UPDATE project_mutation_leases SET expires_at = ? WHERE lease_id = ?",
                    (time.time() + float(ttl_s), lease_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - next beat retries
            pass


# ── universal in-process boundary: every MCP tool call ────────────────────


def _is_switch_call(tool_name: str, arguments: Any) -> bool:
    return _bare(tool_name) == "ai_git" and isinstance(arguments, dict) and (
        str(arguments.get("op") or "").strip().lower() == "switch"
    )


#: SERVER-MEDIATED EXECUTION — the THIRD category, decided by PROVENANCE, never
#: by a host-kind string. This is the label a gate-dispatched call leases under,
#: and therefore the fact a later switch reads.
#:
#: Attestation exists for one risk only: a tool the HOST executes in its own
#: process, which AIDOCS can neither lease nor observe once it has started. An
#: actor that reaches execution through the GATE's dispatch has no such tool in
#: play: its call runs in THIS process and therefore holds the MCP boundary
#: lease (``tool_call_lease``) and, for timed tools, the worker lease —
#: outer_gate_executor dispatches through ``srv.call_tool``, outer_gate_edit
#: runs the leased ``file_ops.str_replace`` engine, and ``project_sync`` takes
#: the root-keyed lease itself.
#:
#: WHY NOT A NAME. The gate's real host-kind contract is
#: ``outer_gate_executor.resolve_web_host_kind``, which mints ``webmcp_<client
#: id>`` per authenticated OAuth client and only falls back to ``generic_mcp``.
#: No fixed set can name that family, and a ``startswith("webmcp_")`` guess
#: would classify by spelling. ``generic_mcp`` is NOT exempt either: it is a
#: FALLBACK for an unidentified client, and MCP-only hooks mean only that AIDOCS
#: governs that host's MCP calls — the host may still have its own shell or
#: editor. ``conductor_worker`` is not exempt for the same reason: a lane worker
#: IS a spawned claude/codex/opencode CLI with native tools of its own.
#:
#: So the fact recorded is the PROVENANCE of the call — it arrived inside a gate
#: dispatch (``current_gate_principal()`` is bound by
#: ``with_gate_execution_scope``) — and it is written durably per host session by
#: the MCP boundary, so a LATER switch can read it for an actor it is not.
SERVER_MEDIATED_PROVENANCE = "gate_dispatch"


@contextmanager
def gate_dispatch_lease(
    project_root: Path | str | None, label: str = SERVER_MEDIATED_PROVENANCE
) -> Iterator[str]:
    """The SHARED lease + invocation id for a GATE-side dispatch.

    ``outer_gate._registry_invoke_edit`` calls ``spec.fn`` DIRECTLY (and
    ``OuterGate.edit`` calls ``outer_gate_edit.apply`` directly): neither goes
    through mcp_server's instrumented ``call_tool``, so neither held the MCP
    boundary lease or set an invocation id. Every registry EDIT tool a WebMCP
    client can call — ai_task / ai_session / ai_backlog / ai_memory / ai_lane —
    reached its project writes that way. ``with_gate_execution_scope`` is the ONE
    seam all three gate dispatch sites share, so the lease lives there.

    Nested inside another in-process lease (the READ executor's
    ``srv.call_tool``) it keeps the OUTER invocation id, so one gate call has one
    id however many seams it crosses."""
    if project_root is None:
        yield ""
        return
    existing = _INVOCATION.get()
    invocation = existing or secrets.token_hex(8)
    inv_token = _INVOCATION.set(invocation)
    _record_server_mediated_provenance(project_root)
    try:
        cm = mutation_lease(
            project_root, f"{label} pid={os.getpid()} invocation={invocation}"
        )
        cm.__enter__()
    except BarrierHeld as exc:
        _INVOCATION.reset(inv_token)
        raise RuntimeError(
            f"AIDOCS branch-transition barrier: {describe(exc.holder)}. This gate call is "
            "refused until the switch finishes. Retry shortly."
        ) from None
    except Exception as exc:  # noqa: BLE001 - a STORE failure, not a held barrier
        _INVOCATION.reset(inv_token)
        # Still fail-closed: with no lease a concurrent switch could sample this
        # tree as quiet. But SAY which failure it was — an unnamed raise reaches
        # the caller as a generic edit_error / index_failed.
        raise RuntimeError(
            f"AIDOCS branch-transition lease store unavailable ({type(exc).__name__}: {exc}); "
            "this gate call is refused closed. This is a STORE failure, not a branch switch."
        ) from None
    except BaseException:
        _INVOCATION.reset(inv_token)
        raise
    try:
        yield invocation
    finally:
        _INVOCATION.reset(inv_token)
        cm.__exit__(None, None, None)


@contextmanager
def tool_call_lease(tool_name: str, arguments: Any, project_root: Path | str | None) -> Iterator[str]:
    """SHARED lease around ONE whole MCP tool call (r0b d7137542 gap 2).

    This is the COMMON BOUNDARY, not a writer list: ``mcp_server``'s
    instrumented ``call_tool`` wraps every in-process tool execution in it, so
    any project file a tool writes -- SessionStore context/journal/plan,
    backlog, memory, config, trash, index -- is written under a lease from
    before the barrier check until the tool returns. Reads hold one too (a tool
    cannot be proven write-free), which only makes a switch wait for them.
    The ONE exemption is ai_git op=switch itself: it takes the EXCLUSIVE side
    and must not drain against its own call."""
    if project_root is None or _is_switch_call(tool_name, arguments):
        _record_server_mediated_provenance(project_root)
        yield ""
        return
    _record_server_mediated_provenance(project_root)
    # Reuse an invocation id already in scope (a gate dispatch that reaches the
    # instrumented call_tool) so ONE call has ONE id across both seams.
    invocation = _INVOCATION.get() or secrets.token_hex(8)
    inv_token = _INVOCATION.set(invocation)
    try:
        cm = mutation_lease(
            project_root,
            f"mcp_tool:{_bare(tool_name)} pid={os.getpid()} invocation={invocation}",
        )
        lease_id = cm.__enter__()
    except BarrierHeld as exc:
        _INVOCATION.reset(inv_token)
        raise RuntimeError(
            f"AIDOCS branch-transition barrier: {describe(exc.holder)}. `{tool_name}` is refused "
            "until the switch finishes. Retry shortly."
        ) from None
    except Exception as exc:  # noqa: BLE001 - a STORE failure, not a held barrier
        _INVOCATION.reset(inv_token)
        raise RuntimeError(
            f"AIDOCS branch-transition lease store unavailable ({type(exc).__name__}: {exc}); "
            f"`{tool_name}` is refused closed. This is a STORE failure, not a branch switch."
        ) from None
    try:
        yield lease_id
    finally:
        _INVOCATION.reset(inv_token)
        cm.__exit__(None, None, None)


def _record_server_mediated_provenance(project_root: Path | str | None) -> None:
    """Record this caller's presence, flagging GATE-ONLY only when the actor is
    STRUCTURALLY gate-only (see ``is_transport_created_web_actor``).

    A gate dispatch by a MIXED-capability host is recorded as ordinary presence:
    one server-mediated call proves nothing about the native Edit/Bash that host
    can still run outside the gate."""
    if project_root is None or not caller_is_server_mediated():
        return
    try:
        from .mcp_server_runtime_helpers import (
            current_calling_host_kind,
            current_calling_host_session_id,
        )

        from .mcp_server_runtime_helpers import current_request_client_binding

        hsid = str(current_calling_host_session_id() or "")
        kind = str(current_calling_host_kind() or "")
        client_id, auth_home = current_request_client_binding()
        record_host_presence(
            project_root,
            kind,
            hsid,
            # GATE-ONLY only when the SERVER's registration says so, and the
            # row REMEMBERS WHICH CLIENT earned it so the claim can be
            # re-validated against the live registration later.
            server_mediated=caller_is_gate_only_surface(),
            client_id=client_id,
            auth_home=auth_home,
        )
    except Exception:  # noqa: BLE001
        pass


# ── host-native tools: PreToolUse / PostToolUse bridge ────────────────────

#: A native (host-executed) tool's lease cannot be renewed -- the hook process
#: that registers it exits at once -- so it is TTL-only (pid NULL). The TTL
#: exceeds the longest native tool run Claude Code allows (Bash max 10 min),
#: and the lease FAILS CLOSED: while it exists a switch refuses, naming it.
NATIVE_TOOL_TTL_S = 1800.0

_NATIVE_READ_TOOLS = frozenset({"read", "grep", "glob", "toolsearch", "ls", "webfetch", "websearch"})


def native_lease_id(host_session_id: str, tool_use_id: str, tool_name: str = "", tool_input: Any = None) -> str:
    """Deterministic lease id. Keyed by the host's ``tool_use_id`` (present on
    Claude Code PreToolUse/PostToolUse payloads); when absent, by a hash of
    (session, tool name, canonical input) so the matching PostToolUse -- which
    carries the same name and input -- releases the same row."""
    import hashlib
    import json as _json

    basis = tool_use_id or (
        str(tool_name) + "\x00" + _json.dumps(tool_input, sort_keys=True, default=str)
    )
    return "native:" + hashlib.sha256(f"{host_session_id}\x00{basis}".encode()).hexdigest()[:40]


def native_tool_needs_lease(tool_name: str) -> bool:
    bare = _bare(tool_name)
    if not bare or bare in _NATIVE_READ_TOOLS:
        return False
    # In-process AIDOCS tools hold their own lease at the tool boundary.
    return not str(tool_name or "").lower().startswith("mcp__aidocs__")


def register_native_tool_lease(
    project_root: Path | str, *, host_session_id: str, tool_use_id: str, tool_name: str, tool_input: Any = None
) -> str:
    """Called by PreToolUse BEFORE it returns allow. Raises BarrierHeld."""
    from .host_concurrency_store import machine_id

    lease_id = native_lease_id(host_session_id, tool_use_id, tool_name, tool_input)
    _register_lease(
        project_root,
        lease_id,
        f"native:{tool_name} tool_use_id={tool_use_id or '-'} host_session={host_session_id or '-'}",
        machine_id(),
        None,
        None,
        NATIVE_TOOL_TTL_S,
    )
    return lease_id


def release_native_tool_lease(
    *, host_session_id: str, tool_use_id: str, tool_name: str = "", tool_input: Any = None
) -> bool:
    return _delete_lease(native_lease_id(host_session_id, tool_use_id, tool_name, tool_input))


# ── per-host native lifecycle attestation (r0b 3e084f64) ──────────────────

#: Hosts whose adapters are wired to the native-tool lease lifecycle:
#: a pre-execution hook that registers the lease BEFORE the tool may run
#: (atomic with the barrier check) and a post-execution hook that releases it.
#:   claude_code -- PreToolUse / PostToolUse / PostToolUseFailure, keyed by the
#:                  host tool_use_id (claude_hook -> hook_pipeline).
#:   opencode    -- plugin pretool / posttool bridge (host_adapter_cli ->
#:                  hook_pipeline), keyed by its call id, else (name, input).
#: A lease whose release never arrives fails CLOSED (TTL), so a flaky post
#: channel costs availability, never exclusivity.
#: NOT attested: openai_agents (on_tool_end carries neither a call id nor the
#: tool input, so a release cannot be matched to its registration) and any host
#: with no adapter (codex, unknown). Their presence on a work tree makes
#: ai_git switch refuse.
ATTESTED_NATIVE_LIFECYCLE_HOSTS: frozenset[str] = frozenset({"claude_code", "opencode"})


#: How long a host's presence on a work tree counts. Same horizon as the
#: native lease: a host seen within it may still have a native tool running.
HOST_PRESENCE_WINDOW_S = NATIVE_TOOL_TTL_S


def record_host_presence(
    project_root: Path | str,
    host_kind: str,
    host_session_id: str,
    *,
    server_mediated: bool = False,
    client_id: str = "",
    auth_home: str = "",
) -> None:
    """Every adapter's pre-execution hook calls this, attested or not. The MCP
    boundary calls it with ``server_mediated=True`` when the call arrived inside
    a gate dispatch — the durable form of that PROVENANCE, so a later switch can
    read it for an actor whose contextvars it cannot see."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO native_host_presence "
            "(root_key, host_kind, host_session_id, last_seen, server_mediated, client_id, auth_home) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                root_key(project_root),
                str(host_kind or "unknown"),
                str(host_session_id or ""),
                time.time(),
                1 if server_mediated else 0,
                str(client_id or ""),
                str(auth_home or ""),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def canonical_host_kind(host_kind: str | None) -> str:
    """Canonical host key ('claude' -> 'claude_code', 'codex' -> 'codex_cli')."""
    try:
        from .host_support_matrix import _canon

        return _canon(host_kind)
    except Exception:  # noqa: BLE001
        return (host_kind or "").strip().lower()


def is_attested_host(host_kind: str | None) -> bool:
    """True only for a host with an attested native-tool lease lifecycle.
    Server-mediated callers are cleared by PROVENANCE, not by kind — see
    ``server_mediated_sessions`` / ``caller_is_server_mediated``."""
    return canonical_host_kind(host_kind) in ATTESTED_NATIVE_LIFECYCLE_HOSTS


def caller_is_server_mediated() -> bool:
    """True when THIS call arrived through the gate's dispatch. NOT a clearance:
    a mixed-capability host can dispatch through the gate and still run a native
    Edit of its own. Clearance needs ``caller_is_gate_only_surface``."""
    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        return current_gate_principal() is not None
    except Exception:  # noqa: BLE001
        return False


def is_transport_created_web_actor(host_session_id: str | None) -> bool:
    """An actor id minted by the WebMCP transport's no-envelope branch.

    NOT A CLEARANCE, and deliberately no longer one (r0b): that branch is taken
    when the client SENDS NO AIDOCS host envelope, and absence is something a
    client chooses. An authenticated native client that simply omits the
    envelope lands here too. It is kept only as a labelling aid; gate-only
    status comes from ``caller_is_gate_only_surface`` — the server's own
    registration record for the authenticated client."""
    try:
        from .webmcp_identity import HOST_SESSION_PREFIX

        return str(host_session_id or "").startswith(HOST_SESSION_PREFIX)
    except Exception:  # noqa: BLE001
        return False


def caller_is_gate_only_surface() -> bool:
    """THIS caller is a client the SERVER has registered as gate-only, calling
    through the gate's dispatch.

    Both halves are server-side facts: the dispatch scope (the gate itself
    entered it) and the surface kind stamped by the transport from the client's
    OAuth REGISTRATION record. Nothing the client sends — or omits — reaches
    either. An unregistered, disabled, unknown or default client is NOT cleared:
    ``unknown`` means "may also run native tools of its own"."""
    if not caller_is_server_mediated():
        return False
    try:
        from .mcp_server_runtime_helpers import current_request_surface_kind
        from .outer_gate_oauth import OAuthStore

        return str(current_request_surface_kind() or "") == OAuthStore.SURFACE_GATE_ONLY
    except Exception:  # noqa: BLE001
        return False


def native_surface_sessions(project_root: Path | str) -> set[str]:
    """Sessions with DIRECT evidence of a native tool surface that is not
    attested. This OUTRANKS any gate-only clearance for the same session: a host
    that has run an unattested native tool has a native surface, whatever else it
    also did."""
    cutoff = time.time() - HOST_PRESENCE_WINDOW_S
    out: set[str] = set()
    for r in _read_rows(
        "SELECT * FROM native_host_presence WHERE root_key = ? AND last_seen >= ?",
        (root_key(project_root), cutoff),
    ):
        if r.get("server_mediated"):
            continue
        if not is_attested_host(str(r.get("host_kind") or "")):
            out.add(str(r.get("host_session_id") or ""))
    return out


def registration_is_still_gate_only(client_id: str, auth_home: str) -> bool:
    """RE-VALIDATE a stored clearance against the CANONICAL authority (r0b).

    A durable positive is never trusted on its own: the clearance is decided
    again HERE, at switch time, through ``OAuthStore.is_cleared_gate_only`` —
    the ONE shared predicate the transport's surface stamp also uses. It
    requires BOTH a registered/enabled client AND
    ``canonical_surface_kind == gate_only``, so a downgrade to ``unknown``, a
    DISABLE, a deleted client, or a client whose canonical capability record is
    missing (the pre-seed upgrade state) invalidates the clearance IMMEDIATELY
    rather than at TTL. The rebuildable ``oauth_clients.surface_kind``
    projection is NOT consulted.
    Anything unresolvable — no client id, no recorded auth home, a missing home,
    an unreadable store — is False."""
    cid = str(client_id or "").strip()
    home = str(auth_home or "").strip()
    if not cid or not home:
        return False
    try:
        from .outer_gate_oauth import OAuthStore

        store = OAuthStore()
        return store.is_cleared_gate_only(Path(home), cid)
    except Exception:  # noqa: BLE001 - unresolvable is not a clearance
        return False


def server_mediated_sessions(project_root: Path | str) -> set[str]:
    """Host sessions that are gate-only RIGHT NOW.

    THE HOST SESSION IS NOT CLIENT-SPECIFIC (r0b): ``webmcp_identity`` digests
    the authenticated user plus the conversation claim, so two DIFFERENT OAuth
    clients of the same user, presenting the same conversation, resolve to the
    SAME web host session. A gate-only client must therefore not be able to
    leave behind a positive that a second, unknown client inherits. Two rules
    close that:

      1. EVERY presence row for the session inside the window must be gate-only.
         Rows are keyed per client, so a second client's ordinary row survives
         alongside the first's and denies the session.
      2. Each of those rows is RE-VALIDATED against the live registration
         (``registration_is_still_gate_only``), so a downgrade, a disable or a
         deleted client invalidates at once, not at TTL.

    Direct evidence of an unattested native surface still outranks everything.
    """
    cutoff = time.time() - HOST_PRESENCE_WINDOW_S
    rows = _read_rows(
        "SELECT * FROM native_host_presence WHERE root_key = ? AND last_seen >= ?",
        (root_key(project_root), cutoff),
    )
    by_session: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sid = str(r.get("host_session_id") or "")
        if sid:
            by_session.setdefault(sid, []).append(r)
    gate_only = {
        sid
        for sid, srows in by_session.items()
        if srows
        and all(
            r.get("server_mediated")
            and registration_is_still_gate_only(
                str(r.get("client_id") or ""), str(r.get("auth_home") or "")
            )
            for r in srows
        )
    }
    return gate_only - native_surface_sessions(project_root)


def host_kind_by_session(project_root: Path | str) -> dict[str, str]:
    """host_session_id -> canonical host kind, from the presence rows each
    adapter writes before it lets a native tool run."""
    cutoff = time.time() - HOST_PRESENCE_WINDOW_S
    out: dict[str, str] = {}
    for r in _read_rows(
        "SELECT * FROM native_host_presence WHERE root_key = ? AND last_seen >= ? ORDER BY last_seen",
        (root_key(project_root), cutoff),
    ):
        sid = str(r.get("host_session_id") or "")
        if sid:
            out[sid] = canonical_host_kind(str(r.get("host_kind") or ""))
    return out


def unattested_hosts(project_root: Path | str) -> list[dict[str, Any]]:
    """Hosts seen on this work tree within the window that lack an attested
    native-tool lifecycle channel. Raises on store failure (caller refuses)."""
    cutoff = time.time() - HOST_PRESENCE_WINDOW_S
    rows = _read_rows(
        "SELECT * FROM native_host_presence WHERE root_key = ? AND last_seen >= ?",
        (root_key(project_root), cutoff),
    )
    return [
        r
        for r in rows
        if not is_attested_host(str(r.get("host_kind") or "")) and not r.get("server_mediated")
    ]


# ── worker-lifetime lease for timed tools (r0b 5620f244) ──────────────────


def acquire_worker_lease(tool: str, kwargs: dict[str, Any]):
    """Take a shared lease in the SUBMITTING thread and return its release
    callable, to be invoked by the WORKER when the work actually finishes.

    A timed tool's RPC can time out and return while its worker keeps running
    (the Future is parked, not cancelled), so the RPC-scoped ``tool_call_lease``
    ends too early. This lease overlaps the RPC lease and lives exactly as long
    as the worker. Raises RuntimeError when another caller holds the barrier."""
    root = (kwargs or {}).get("project_root") or (kwargs or {}).get("root")
    if not root:
        try:
            from .mcp_server_runtime_helpers import resolve_project_root

            root = resolve_project_root()
        except Exception:  # noqa: BLE001 - no bound project: nothing to switch
            root = None
    if not root or _is_switch_call(tool, kwargs):
        return lambda: None
    # Same invocation id as the RPC-scoped lease, so the two leases of ONE
    # invocation are matchable as such.
    cm = mutation_lease(
        root,
        f"mcp_worker:{_bare(tool)} pid={os.getpid()} "
        f"invocation={current_tool_invocation_id() or secrets.token_hex(8)}",
    )
    try:
        cm.__enter__()
    except BarrierHeld as exc:
        raise RuntimeError(
            f"AIDOCS branch-transition barrier: {describe(exc.holder)}. `{tool}` is refused "
            "until the switch finishes. Retry shortly."
        ) from None
    lock = threading.Lock()
    done = {"v": False}

    def release() -> None:
        with lock:
            if done["v"]:
                return
            done["v"] = True
        cm.__exit__(None, None, None)

    return release


def release_native_session_leases(host_session_id: str) -> int:
    """Stop / SubagentStop: no tool of that host session is still running."""
    if not host_session_id:
        return 0
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM project_mutation_leases WHERE lease_id LIKE 'native:%' AND owner LIKE ?",
                (f"% host_session={host_session_id}",),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return 0


def governed_project_mutation(label: str, *, root_arg: str = "project_root", error_factory=None):
    """Decorator: run the wrapped mutation under a shared lease keyed on its
    ``root_arg`` argument (positional or keyword). ``error_factory(exc)``
    converts a BarrierHeld refusal into the caller's own error type."""

    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                root = sig.bind_partial(*args, **kwargs).arguments.get(root_arg)
            except TypeError:
                root = None
            if root is None:
                return fn(*args, **kwargs)
            try:
                lease = mutation_lease(root, f"{label} pid={os.getpid()}")
                lease.__enter__()
            except BarrierHeld as exc:
                if error_factory is not None:
                    raise error_factory(exc) from None
                raise BarrierHeld(exc.holder) from None
            try:
                return fn(*args, **kwargs)
            finally:
                # NULL exc-info on purpose: contextlib re-raises by assigning
                # ``exc.__traceback__``, which frozen-dataclass exceptions
                # refuse; the lease only needs its finally to run.
                lease.__exit__(None, None, None)

        return wrapper

    return deco


# ── pretool / enforce consumers ───────────────────────────────────────────

_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read",
        "grep",
        "glob",
        "toolsearch",
        "ai_find",
        "ai_bundle",
        "ai_get_lines",
        "ai_get_symbol_snippet",
        "ai_get_symbol_info",
        "ai_get_outline",
        "ai_get_dependencies",
        "ai_get_module_files",
        "ai_get_modules",
        "ai_investigate",
        "ai_trace",
        "ai_text_search",
        "ai_search",
        "ai_index_status",
        "ai_whoami",
        "ai_version",
        "ai_gate_explain",
    }
)


def _bare(tool_name: str) -> str:
    norm = str(tool_name or "").strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if norm.startswith(prefix):
            return norm[len(prefix):]
    return norm


def is_barrier_exempt(tool_name: str, tool_input: Any = None) -> bool:
    """Reads and freeze-remedy/report tools may run while the barrier is held."""
    bare = _bare(tool_name)
    if bare in _READ_TOOLS:
        return True
    try:
        from .operation_classes import freeze_remedy_reachable

        return bool(freeze_remedy_reachable(tool_name, tool_input))
    except Exception:  # noqa: BLE001
        return False


def barrier_refusal(project_root: Path | str | None, tool_name: str, tool_input: Any = None) -> str:
    """'' when the call may proceed; otherwise the refusal reason."""
    if project_root is None or is_barrier_exempt(tool_name, tool_input):
        return ""
    try:
        row = active_barrier(project_root)
    except Exception as exc:  # noqa: BLE001 - fail closed for mutations
        return (
            f"AIDOCS branch-transition barrier: the barrier store could not be read ({exc!r}); "
            f"refusing `{tool_name}` rather than risk mutating the tree during a branch switch."
        )
    if row is None or _is_holder(row):
        return ""
    return (
        f"AIDOCS branch-transition barrier: {describe(row)}. `{tool_name}` could change files "
        "the switch is moving; it is refused until the switch finishes. Reads (ai_find / "
        "ai_get_lines / Read / Grep ...) may proceed. Retry shortly."
    )


def egress_barrier_refusal(cwd: str | None, reachability: str = "agent_reachable") -> str:
    """'' unless ``cwd`` is inside a project whose barrier another caller holds."""
    try:
        row = active_barrier(cwd or os.getcwd())
    except Exception as exc:  # noqa: BLE001
        if reachability == "agent_reachable":
            return f"branch-transition barrier store unreadable ({exc!r}); refusing closed"
        return ""
    if row is None or _is_holder(row):
        return ""
    return (
        f"AIDOCS branch-transition barrier: {describe(row)}; governed subprocesses in "
        "this project are refused until it finishes."
    )
