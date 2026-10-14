"""Durable write guard for multi-file batch edits (r0b2 66a50b92-ccb, design v3).

A batch writes its files in place, one after another. An I/O failure (or a
crash) part-way leaves some files written and the failing one possibly torn.
Any read evidence an agent holds for those files is then stale, in every
session and every process, and so is the code index. This module records
that, durably, BEFORE the first byte is written:

    arm(batch, paths)     before the first write; the batch is refused if it fails
    settle(batch, ...)    after the writes: written / untouched paths drop their
                          guard, the path that failed becomes UNCERTAIN
    blocked(path, sess)   the edit side: True while any guard on the path is ARMED
                          (in flight) or UNCERTAIN and not cleared by this session;
                          None when the guard store cannot be read (refuse)
    read_begin / clear    the read side: a genuine disk read (ai_get_lines,
                          ai_read_raw) snapshots the UNCERTAIN guards at its
                          start and, once it succeeded, clears exactly those for
                          its session. A read that started while the write was in
                          flight cannot clear it.
    mark_index_verified   the code index row of the path was verified against
                          the bytes on disk (B2); until then index_dirty() names it

Ordering comes from causality, not clocks or event ids: a clear names guard
ids that existed when the read began. An ARMED guard whose owner process is
gone, or whose owner is this process with the batch no longer in flight,
becomes UNCERTAIN (crash-orphan recovery); it is never cleared directly.

Scope: only the two batch engines (file_ops.batch_edit / batch_str_replace,
called by the ai_batch_edit wrappers) arm guards. Edit refusal applies to
identified (managed) sessions; an unidentified caller on an unmanaged project
has no read-before-edit rule to strengthen, and nothing here claims it does.
Single-file editors and extract_block are not guarded.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path

from ._sqlite_connect import Durability, connect

_DB_REL = (".MEMORY", ".index", "write_guards.sqlite3")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS guards (
    guard_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    path_key TEXT NOT NULL,
    path TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('armed', 'uncertain', 'written')),
    owner_pid INTEGER NOT NULL,
    owner_started TEXT NOT NULL,
    index_verified INTEGER NOT NULL DEFAULT 0,
    armed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS guards_path ON guards(project, path_key);
CREATE TABLE IF NOT EXISTS guard_clears (
    guard_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    PRIMARY KEY (guard_id, session_key)
);
CREATE TABLE IF NOT EXISTS guard_reads (
    guard_id INTEGER NOT NULL,
    session_key TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS guard_reads_key ON guard_reads(guard_id, session_key)
"""

_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()
_SCHEMA_READY: set[tuple[str, str]] = set()  # (db path, incarnation) set up by this process
_SCHEMA_LOCK = threading.Lock()


class GuardUnavailable(RuntimeError):
    """The guard store could not be read or written."""

    reason = "batch.write_guard_unavailable"


class GuardConflict(GuardUnavailable):
    """Another batch's write to one of these paths is still in flight."""

    reason = "batch.path_busy"

    def __init__(self, paths: list[str]) -> None:
        self.paths = list(paths)
        super().__init__("another batch is still writing " + ", ".join(self.paths))


def _db_path(project_root: Path) -> Path:
    return Path(project_root).joinpath(*_DB_REL)


def _project_key(project_root: Path) -> str:
    return os.path.normcase(str(Path(project_root).resolve()))


def path_key(project_root: Path, path: str) -> tuple[str, str]:
    """(match key, canonical display path) for a project path. Resolved, so a
    junction / symlink / ``./`` / case alias of one file is one key."""
    root = Path(project_root).resolve()
    raw = Path(str(path).replace("\\", "/"))
    target = (raw if raw.is_absolute() else root / raw).resolve()
    try:
        canonical = target.relative_to(root).as_posix()
    except ValueError:
        canonical = target.as_posix()
    return os.path.normcase(canonical), canonical


def _marker_path(project_root: Path) -> Path:
    """Sidecar written before the first guard is ever armed. If the guard
    database is missing while this exists, its state was LOST - that reads
    as unknown (refuse), never as "no guards" (r0b2 021a34d1-d45). An
    operator who deliberately resets the store removes both."""
    return _db_path(project_root).with_name("write_guards.present")


def _store_lost(project_root: Path) -> bool:
    return not _db_path(project_root).is_file() and _marker_path(project_root).is_file()


def _incarnation(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute("SELECT value FROM guard_meta WHERE key = 'incarnation'").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def _migrate_guard_reads(conn: sqlite3.Connection) -> None:
    """A store written before page fingerprints (r0b2 d675b06c-598): its
    guard_reads rows carry no content version, so they can never prove a
    consistent re-read. Drop them (that only REMOVES clearance - an
    uncertain guard keeps blocking until a fresh, versioned re-read) and let
    the schema recreate the table with the fingerprint column."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(guard_reads)").fetchall()}
    if cols and "fingerprint" not in cols:
        conn.execute("DROP TABLE guard_reads")
        # ...and every clearance those versionless reads granted (r0b2
        # 6e212abf-299): a v4 clear could come from pages of two different
        # content versions. Revoking it only re-requires a fresh, versioned
        # whole-file read; guards themselves and their index dirt are kept.
        has_clears = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'guard_clears'"
        ).fetchone()
        if has_clears:
            conn.execute("DELETE FROM guard_clears")


def _open(project_root: Path) -> sqlite3.Connection:
    """Open the guard store. The schema runs once per process per database
    INCARNATION - a random id stored inside the database itself, so a
    recreated file gets its schema again while checkpoints and metadata
    changes (st_ctime on Linux) do not (r0b2 b9c7ed4f-bd6). First-time setup
    is serialised: across threads by a process lock, across processes by an
    IMMEDIATE transaction around the DDL."""
    db = _db_path(project_root)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db, durability=Durability.AUDIT, row_factory=False, timeout=5.0, busy_timeout_ms=5000)
        current = _incarnation(conn)
        if current is not None and (str(db), current) in _SCHEMA_READY:
            return conn
        with _SCHEMA_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate_guard_reads(conn)
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute("CREATE TABLE IF NOT EXISTS guard_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute(
                    "INSERT OR IGNORE INTO guard_meta (key, value) VALUES ('incarnation', ?)", (uuid.uuid4().hex,),
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                raise
            _SCHEMA_READY.add((str(db), str(_incarnation(conn))))
        return conn
    except (OSError, sqlite3.Error) as exc:
        raise GuardUnavailable(f"write guard store unavailable: {exc}") from exc


def _process_started(pid: int) -> str | None:
    """An identity for process ``pid`` that changes if the pid is reused:
    its creation time. None when no such process is running."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:  # STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return f"{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
        finally:
            k32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[1].split()[19]
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return "alive"


_SELF = (os.getpid(), _process_started(os.getpid()) or "unknown")


def _owner_live(pid: int, started: str) -> bool:
    if (pid, started) == _SELF:
        return True
    current = _process_started(pid)
    return current is not None and current == started


def arm(project_root: Path, batch_id: str, paths: list[str]) -> None:
    """Record, durably, that ``batch_id`` is about to write ``paths``. ONE
    in-flight batch owns a path (r0b2 db046e49-5f4): in the same IMMEDIATE
    transaction, an ARMED guard of another batch on any of these paths (after
    crash-orphan recovery) refuses with GuardConflict. Raises GuardUnavailable
    if the record cannot be committed. Either way the caller writes nothing."""
    project = _project_key(project_root)
    rows = [(project, *path_key(project_root, p), batch_id, _SELF[0], _SELF[1], time.time()) for p in paths]
    if _store_lost(project_root):
        raise GuardUnavailable("the write guard store is missing but guards were armed here: their state is lost")
    marker = _marker_path(project_root)
    try:
        if not marker.is_file():
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                "Write guards were armed in this project. If write_guards.sqlite3 is missing while this\n"
                "file exists, guarded edits are refused (their state is lost). Remove both to reset.\n",
                encoding="utf-8",
            )
    except OSError as exc:
        raise GuardUnavailable(f"write guard marker could not be written: {exc}") from exc
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.add(batch_id)
    conn = _open(project_root)
    try:
        conn.execute("BEGIN IMMEDIATE")
        busy: list[str] = []
        for _project, key, display, *_rest in rows:
            existing = _guard_rows(conn, project, key)
            _recover_orphans(conn, existing)
            if any(r[1] == "armed" for r in _guard_rows(conn, project, key)):
                busy.append(display)
        if busy:
            conn.rollback()
            raise GuardConflict(busy)
        conn.executemany(
            "INSERT INTO guards (project, path_key, path, batch_id, state, owner_pid, owner_started, armed_at)"
            " VALUES (?, ?, ?, ?, 'armed', ?, ?, ?)",
            rows,
        )
        conn.commit()
    except GuardConflict:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(batch_id)
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(batch_id)
        raise GuardUnavailable(f"write guard could not be armed: {exc}") from exc
    finally:
        conn.close()


def settle(
    project_root: Path, batch_id: str, *, written_paths: list[str], uncertain_paths: list[str],
) -> None:
    """After the writes. ``uncertain_paths`` (the file whose write failed)
    become UNCERTAIN; ``written_paths`` become WRITTEN (no edit block, but the
    index stays dirty until mark_index_verified); every other guard of the
    batch (files never written) is dropped. On failure the guards stay ARMED
    (blocking) and this raises."""
    project = _project_key(project_root)
    keep = {path_key(project_root, p)[0] for p in uncertain_paths}
    done = {path_key(project_root, p)[0] for p in written_paths} - keep
    try:
        conn = _open(project_root)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for guard_id, key in conn.execute(
                "SELECT guard_id, path_key FROM guards WHERE project = ? AND batch_id = ?", (project, batch_id),
            ).fetchall():
                if key in keep:
                    conn.execute("UPDATE guards SET state = 'uncertain' WHERE guard_id = ?", (guard_id,))
                elif key in done:
                    conn.execute("UPDATE guards SET state = 'written' WHERE guard_id = ?", (guard_id,))
                else:
                    conn.execute("DELETE FROM guards WHERE guard_id = ?", (guard_id,))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise GuardUnavailable(f"write guard could not be settled: {exc}") from exc
        finally:
            conn.close()
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(batch_id)


def _recover_orphans(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """ARMED guards whose writer is provably no longer writing become UNCERTAIN:
    the owner process is gone, or it is this process and the batch is no longer
    in flight (its settle failed or never ran)."""
    with _IN_FLIGHT_LOCK:
        in_flight = set(_IN_FLIGHT)
    for guard_id, state, batch_id, pid, started in rows:
        if state != "armed":
            continue
        own = (pid, started) == _SELF
        if (own and batch_id not in in_flight) or (not own and not _owner_live(pid, started)):
            conn.execute("UPDATE guards SET state = 'uncertain' WHERE guard_id = ? AND state = 'armed'", (guard_id,))


def _guard_rows(conn: sqlite3.Connection, project: str, key: str) -> list[tuple]:
    return conn.execute(
        "SELECT guard_id, state, batch_id, owner_pid, owner_started FROM guards WHERE project = ? AND path_key = ?",
        (project, key),
    ).fetchall()


def blocked(project_root: Path, path: str, session_key: str) -> bool | None:
    """True if editing ``path`` must be refused for ``session_key``: a guard on
    it is ARMED, or UNCERTAIN and not cleared by this session. None if the
    guard store cannot be read (the caller refuses)."""
    if _store_lost(project_root):
        return None  # guards existed and their store is gone: refuse, never "clear"
    if not _db_path(project_root).is_file():
        return False
    project = _project_key(project_root)
    key = path_key(project_root, path)[0]
    try:
        conn = _open(project_root)
    except GuardUnavailable:
        return None
    try:
        rows = _guard_rows(conn, project, key)
        if not rows:
            return False
        conn.execute("BEGIN IMMEDIATE")
        _recover_orphans(conn, rows)
        conn.commit()
        rows = _guard_rows(conn, project, key)
        for guard_id, state, *_ in rows:
            if state == "written":
                continue
            if state == "armed":
                return True
            cleared = conn.execute(
                "SELECT 1 FROM guard_clears WHERE guard_id = ? AND session_key = ?", (guard_id, session_key),
            ).fetchone()
            if cleared is None:
                return True
        return False
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def read_begin(project_root: Path, path: str) -> list[int] | None:
    """At the START of a disk read: the UNCERTAIN guard ids on ``path`` that
    this read may clear once it succeeds. ARMED guards are excluded - their
    write is still in flight. None if the store cannot be read."""
    if _store_lost(project_root):
        return None
    if not _db_path(project_root).is_file():
        return []
    project = _project_key(project_root)
    key = path_key(project_root, path)[0]
    try:
        conn = _open(project_root)
    except GuardUnavailable:
        return None
    try:
        rows = _guard_rows(conn, project, key)
        if any(r[1] == "armed" for r in rows):
            conn.execute("BEGIN IMMEDIATE")
            _recover_orphans(conn, rows)
            conn.commit()
            rows = _guard_rows(conn, project, key)
        return [r[0] for r in rows if r[1] == "uncertain"]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _covers(ranges: list[tuple[int, int]], total: int) -> bool:
    """Do the 1-based inclusive line ranges cover 1..total?"""
    if total <= 0:
        return True
    reach = 0
    for start, end in sorted(ranges):
        if start > reach + 1:
            return False
        reach = max(reach, end)
        if reach >= total:
            return True
    return False


def file_fingerprint(project_root: Path, path: str) -> str | None:
    """sha256 of the file's bytes NOW (None if unreadable): the content
    version a disk read saw, when taken before and after it and equal."""
    import hashlib

    canonical = path_key(project_root, path)[1]
    target = Path(canonical) if Path(canonical).is_absolute() else Path(project_root) / canonical
    try:
        return hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return None


def clear(
    project_root: Path,
    guard_ids: list[int],
    session_key: str,
    *,
    fingerprint: str | None,
    start: int | None = None,
    end: int | None = None,
    total: int | None = None,
    full: bool = False,
) -> bool:
    """After a SUCCESSFUL disk read that began with ``read_begin``: record what
    ``session_key`` has now seen of those guards' path. ``fingerprint`` is the
    content version the read saw (file_fingerprint equal before and after it;
    None if they differed or could not be taken - then nothing is recorded).

    A guard is CLEARED for the session only once its post-guard reads cover
    the WHOLE file AT ONE VERSION (r0b2 db046e49-5f4, 021a34d1-d45): lines
    ``start``..``end`` of a ``total``-line file accumulate across reads of the
    SAME fingerprint; a read of a different version discards the session's
    earlier pages first, so a union of pages from two versions never clears.
    ``full`` (a complete raw read) covers everything at once. False if the
    store fails (nothing recorded; the guards keep blocking)."""
    if not guard_ids or not fingerprint:
        return True  # nothing (consistent) was read: nothing recorded
    if not full and (start is None or end is None or total is None):
        return True  # nothing measurable was read: nothing recorded
    try:
        conn = _open(project_root)
    except GuardUnavailable:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        for gid in guard_ids:
            live = conn.execute(
                "SELECT 1 FROM guards WHERE guard_id = ? AND state = 'uncertain'", (gid,),
            ).fetchone()
            if live is None:
                continue
            conn.execute(  # pages of another version are worthless now
                "DELETE FROM guard_reads WHERE guard_id = ? AND session_key = ? AND fingerprint != ?",
                (gid, session_key, fingerprint),
            )
            if not full:
                conn.execute(
                    "INSERT INTO guard_reads (guard_id, session_key, start_line, end_line, fingerprint)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (gid, session_key, int(start), int(end), fingerprint),
                )
                ranges = conn.execute(
                    "SELECT start_line, end_line FROM guard_reads WHERE guard_id = ? AND session_key = ?",
                    (gid, session_key),
                ).fetchall()
                if not _covers([(int(a), int(b)) for a, b in ranges], int(total)):
                    continue
            conn.execute(
                "INSERT OR IGNORE INTO guard_clears (guard_id, session_key) VALUES (?, ?)", (gid, session_key),
            )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_index_verified(project_root: Path, path: str) -> bool:
    """The index row of ``path`` was verified against the bytes on disk."""
    project = _project_key(project_root)
    key = path_key(project_root, path)[0]
    try:
        conn = _open(project_root)
    except GuardUnavailable:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE guards SET index_verified = 1 WHERE project = ? AND path_key = ? AND state != 'armed'",
            (project, key),
        )
        # A WRITTEN guard exists only for the index; once verified it is done.
        conn.execute(
            "DELETE FROM guards WHERE project = ? AND path_key = ? AND state = 'written' AND index_verified = 1",
            (project, key),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()


def verify_index_row(project_root: Path, path: str) -> bool:
    """True only when the code index holds ``path`` as it is on disk NOW: the
    row exists, is parsed, and its checksum equals the indexer's own checksum
    of the current bytes (blast_envelope.file_checksum: read_text with
    errors="ignore", the normalisation the indexer stores). A path no index
    language tracks has no row to hold and counts as verified. A refused,
    missing, unparsed or mismatched row of a tracked path does not."""
    from .blast_envelope import file_checksum
    from .code_index_store import CodeIndexStore
    from .language_descriptors import language_for_custom_descriptor

    canonical = path_key(project_root, path)[1]
    try:
        with CodeIndexStore().connect(project_root) as conn:
            row = conn.execute(
                "SELECT checksum, parsed FROM code_files WHERE path = ? LIMIT 1", (canonical,),
            ).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable index is not verified
        return False
    if row is None:
        try:
            tracked = language_for_custom_descriptor(project_root, canonical, Path(canonical).suffix.lower())
        except Exception:  # noqa: BLE001
            return False
        return tracked is None
    disk = file_checksum(project_root, canonical)
    return bool(disk) and int(row[1] or 0) == 1 and str(row[0]) == disk


def reverify_dirty(project_root: Path) -> list[str]:
    """For a reconcile that just finished: re-verify every dirty path and mark
    the ones whose index row now matches disk. Returns the paths still dirty.
    A path whose row is refused or absent stays dirty (r0b2 09788453-f2e)."""
    dirty = index_dirty(project_root)
    if not dirty:
        return []
    still = []
    for path in dirty:
        if not (verify_index_row(project_root, path) and mark_index_verified(project_root, path)):
            still.append(path)
    return still


def index_dirty(project_root: Path) -> list[str] | None:
    """Paths whose code-index row is not verified against disk since a guarded
    write (durable, every process sees it). None if the store cannot be read."""
    if _store_lost(project_root):
        return None  # guards existed and their store is gone: unknown, never clean
    if not _db_path(project_root).is_file():
        return []
    try:
        conn = _open(project_root)
    except GuardUnavailable:
        return None
    try:
        rows = conn.execute(
            "SELECT DISTINCT path FROM guards WHERE project = ? AND index_verified = 0 ORDER BY path",
            (_project_key(project_root),),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.Error:
        return None
    finally:
        conn.close()
