"""The operator's word to MOVE a conversation: ``connect project X`` /
``connect session X`` (#1166, operator 2026-10-18: "ai project and ai session
are user-intent tools because they change agent permissions/scope").

WHY NOT ``session_query_gate.user_intent_actions``. That store (bleed B) keys
grants to the managed session the caller is ALREADY bound to; it serves
``ai_session create|delete``. A move is made by a conversation that may be
bound to nothing on the target (the very state ``connect`` repairs), so its
grant is keyed to the HOST conversation (``host_session_id``) in the
install-wide DB, the same one as ``window_home_store`` /
``tool_admission_store`` (``~/.aidocs/config.sqlite3``,
``AIDOCS_GLOBAL_CONFIG_DB`` in tests).

LIFETIME. Each UserPromptSubmit REPLACES the conversation's grants with the
ones its prompt states (so last turn's word never outlives the turn that
followed it); a grant is consumed by the act it names (single use) and is
dead after ``BINDING_INTENT_TTL_S`` regardless.

NOT AN AUTHORITY on its own: the move is still checked by its own rules
(require_cross_project, the managed-session gate); this only answers "did the
operator ask for exactly this move".
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect

BINDING_INTENT_TABLE = "binding_intent"

#: Design decision (2026-10-19, #1166 A1): a grant has 2 minutes to be used
#: and decays after use. A stated move is spendable for 2 minutes, once
#: (consume deletes it), and the next prompt revokes it regardless -- so the
#: residual r0b1 named (a previous turn's background tool spending that turn's
#: word after a double store fault) is bounded by this window.
BINDING_INTENT_TTL_S = 2 * 60

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {BINDING_INTENT_TABLE} (
    host_session_id TEXT NOT NULL,
    tool            TEXT NOT NULL,
    target          TEXT NOT NULL,
    minted_at       REAL NOT NULL,
    generation      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (host_session_id, tool, target)
)
"""

#: #1166 blocker A (r0b1 7f12fa71-126 / 9d47e7aa-834): the conversation's PROMPT
#: GENERATION. Every UserPromptSubmit bumps it FIRST -- before any branch that
#: returns early (/aidocs) or raises (an unservable selection) -- and deletes the
#: previous prompt's unspent grants in the same transaction. A grant is spent
#: only while its generation is still the current one.
GENERATION_TABLE = "binding_prompt_generation"

_GEN_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {GENERATION_TABLE} (
    host_session_id TEXT PRIMARY KEY,
    generation      INTEGER NOT NULL,
    bumped_at       REAL NOT NULL
)
"""


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_GEN_SCHEMA)
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({BINDING_INTENT_TABLE})")}
    if "generation" not in cols:  # a table created before blocker A
        conn.execute(f"ALTER TABLE {BINDING_INTENT_TABLE} ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")


#: The conversation's revocation LATCH lives in its lock file, OUTSIDE the grant
#: DB: byte 0 is b"1" while a revocation is pending, b"0" (or nothing) once
#: one committed. The OS lock sits on a byte past it, so reading and writing the
#: latch never touch the locked range.
_LATCH_PENDING, _LATCH_CLEAR = b"1", b"0"
_LOCK_OFFSET = 1 << 20


def _lock_path(host_session_id: str) -> Path:
    import hashlib

    digest = hashlib.sha256(host_session_id.encode("utf-8")).hexdigest()[:24]
    return _global_db_path().parent / "binding-revocation" / f"{digest}.lock"


@contextmanager
def _host_lock(host_session_id: str) -> Iterator[object]:
    """The conversation's EXCLUSIVE interprocess lock (r0b1 77914355-7d9 A2),
    yielding the open lock file. begin_prompt holds it from setting the latch to
    clearing it, and every spend holds it around its read-and-delete, so a spend
    and a revocation never interleave and no prompt ever sees another's half-
    done revocation. The OS releases it when its process dies, so a crash never
    leaves it held. Raises when it cannot be taken (the caller refuses)."""
    path = _lock_path(host_session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)), "r+b", buffering=0)
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # waits ~10 s, then raises
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield fh
        finally:
            if os.name == "nt":
                fh.seek(_LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _set_latch(fh, value: bytes) -> None:
    """Write the latch durably (fsync'd); raises on any failure."""
    fh.seek(0)
    fh.write(value)
    os.fsync(fh.fileno())


def _latch_pending(fh) -> bool:
    """True while a revocation is pending -- or the latch cannot be read
    (unknown is never "clear")."""
    try:
        fh.seek(0)
        return fh.read(1) not in (b"", _LATCH_CLEAR)
    except Exception:  # noqa: BLE001 -- an unreadable latch denies
        return True


def begin_prompt(host_session_id: str) -> int | None:
    """begin_prompt_checked without the safety verdict (direct callers, tests)."""
    return begin_prompt_checked(host_session_id)[0]


def begin_prompt_checked(host_session_id: str) -> tuple[int | None, bool]:
    """FIRST step of every UserPromptSubmit (blocker A): revoke the previous
    prompt's unspent move grants and bump the prompt generation.

    Returns ``(generation, safe)``. ``safe`` is False ONLY when no durable deny
    state could be written at all -- neither the latch nor the revocation --
    so the previous prompt's grant is still spendable once the store recovers
    (r0b1 c33d21bb-4b8). The UPS hook then BLOCKS the prompt: no new turn runs
    on the old word. Stated residual: work the PREVIOUS turn itself started (a
    background tool) can still spend the word that turn was given, within its
    TTL -- the consent's own scope, never a later turn's.

    Under the conversation's exclusive lock (r0b1 77914355-7d9): (1) the latch
    is set to PENDING and fsync'd BEFORE the database is touched; (2) one BEGIN
    IMMEDIATE transaction deletes the conversation's grants and bumps its
    generation; (3) only after that commit is the latch cleared. Any failure
    leaves the latch PENDING, and while it is pending NO grant of this
    conversation can be spent -- the SPEND side refuses, so this holds for an
    old turn, a background tool or a direct MCP call too, not only for the
    next model turn. A crash anywhere leaves it pending until a later prompt's
    revocation commits; under the lock no other revocation can be in flight,
    so clearing it then never hides a live one.

    A generation of None means this prompt mints nothing. The latch write
    failing still runs the revocation (so the old grants are gone); a lock that
    cannot be taken still attempts the revocation without it. Never raises."""
    host = str(host_session_id or "").strip()
    if not host:
        return None, True
    try:
        with _host_lock(host) as fh:
            try:
                _set_latch(fh, _LATCH_PENDING)
                latched = True
            except Exception:  # noqa: BLE001 -- still revoke; mint nothing
                latched = False
            try:
                generation = _revoke_and_bump(host)
            except Exception:  # noqa: BLE001 -- safe only if the latch holds the deny
                return None, latched
            if not latched:
                return None, True  # revoked: nothing old is left to spend
            try:
                _set_latch(fh, _LATCH_CLEAR)
            except Exception:  # noqa: BLE001 -- stays pending: every spend refuses (safe)
                return None, True
            return generation, True
    except Exception:  # noqa: BLE001 -- no lock: no latch; the revocation is still tried
        try:
            _revoke_and_bump(host)
            return None, True
        except Exception:  # noqa: BLE001 -- nothing durable could be written
            return None, False


def _revoke_and_bump(host: str) -> int:
    db_path = _global_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _canonical_connect(db_path)
    try:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DELETE FROM {BINDING_INTENT_TABLE} WHERE host_session_id = ?", (host,))
        row = conn.execute(
            f"SELECT generation FROM {GENERATION_TABLE} WHERE host_session_id = ?", (host,)
        ).fetchone()
        generation = (int(row[0]) if row else 0) + 1
        conn.execute(
            f"INSERT OR REPLACE INTO {GENERATION_TABLE} (host_session_id, generation, bumped_at) "
            "VALUES (?, ?, ?)",
            (host, generation, time.time()),
        )
        conn.commit()
        return generation
    finally:
        conn.close()


#: The session a conversation was last in, per project (#1166, r0b1
#: 578622ff-064): "currently unbound" is not "new". A lapsed bind, a /clear or
#: an unbind must not turn the next connect into a free pick -- resuming THIS
#: session stays free, anything else needs the operator's word. Never deleted
#: by unbind or expiry; overwritten only by the next successful connect.
SESSION_HISTORY_TABLE = "conversation_last_session"

_HISTORY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {SESSION_HISTORY_TABLE} (
    host_session_id TEXT NOT NULL,
    project         TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    connected_at    REAL NOT NULL,
    PRIMARY KEY (host_session_id, project)
)
"""


def record_session_connect(host_session_id: str, project_root: object, session_id: str) -> bool:
    """Remember that this conversation is now in ``session_id`` on this
    project. True when written; never raises."""
    host = str(host_session_id or "").strip()
    project = normalize_target(project_root)
    sid = str(session_id or "").strip()
    if not (host and project and sid):
        return False
    try:
        db_path = _global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db_path)
        try:
            conn.execute(_HISTORY_SCHEMA)
            conn.execute(
                f"INSERT OR REPLACE INTO {SESSION_HISTORY_TABLE} "
                "(host_session_id, project, session_id, connected_at) VALUES (?, ?, ?, ?)",
                (host, project, sid, time.time()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- no record: the next connect needs the word
        return False


def last_session_connect(host_session_id: str, project_root: object) -> str:
    """The session this conversation was last in on this project, or ''."""
    host = str(host_session_id or "").strip()
    project = normalize_target(project_root)
    db_path = _global_db_path()
    if not (host and project) or not db_path.is_file():
        return ""
    try:
        conn = _canonical_connect(db_path)
        try:
            row = conn.execute(
                f"SELECT session_id FROM {SESSION_HISTORY_TABLE} "
                "WHERE host_session_id = ? AND project = ?",
                (host, project),
            ).fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- "no table yet" included
        return ""
    return str(row[0]) if row and row[0] else ""


def _global_db_path() -> Path:
    override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "config.sqlite3"


def normalize_target(value: object) -> str:
    """One spelling for comparing a stated target with the act's target:
    no quotes / trailing punctuation, forward slashes, no trailing slash,
    case-folded (Windows paths and project names are case-insensitive)."""
    text = str(value or "").strip().strip("`'\"“”‘’").rstrip(".,;:!?)").strip()
    return text.replace("\\", "/").rstrip("/").lower()


#: "... but not y": a session of the project the operator EXCLUDED. It is never
#: spent; it only stops an ANY-session grant (``<project>#*``) from reaching y.
SESSION_DENY_TOOL = "ai_session_deny"
SESSION_ANY = "*"


def session_key(project: object, session_id: object) -> str:
    """The one spelling of "session <id> of project <project>" -- '' when
    either half is missing, so an unscoped session grant can never exist."""
    proj, sid = normalize_target(project), normalize_target(session_id)
    return f"{proj}#{sid}" if proj and sid else ""


def _canonical_root(value: object) -> str:
    try:
        return normalize_target(Path(str(value)).resolve())
    except (OSError, ValueError):
        return ""


class RegistryUnreadable(Exception):
    """A project registry could not be read: no target can be resolved."""


def _registry_rows(conn, sql: str) -> list[tuple]:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []  # a registry nothing was ever written to: empty, not broken
        raise


def _registered_roots() -> set[str]:
    """Every project root AIDOCS has on record (#1166 B, r0b1 77914355-7d9):
    the known-projects registry and the commission registry (every adopted /
    commissioned project). Both live in this install's own stores, so the
    install is the tenant scope. Raises RegistryUnreadable when EITHER cannot
    be read: a registry fault must never make an ambiguous name look unique."""
    roots: set[str] = set()
    try:
        db_path = _global_db_path()
        if db_path.is_file():
            conn = _canonical_connect(db_path)
            try:
                rows = _registry_rows(conn, "SELECT project_root FROM known_projects")
            finally:
                conn.close()
            roots |= {_canonical_root(r[0]) for r in rows}
        from . import project_commission as _pc

        conn = _pc._conn()
        try:
            rows = _registry_rows(conn, "SELECT project_root FROM project_commission")
        finally:
            conn.close()
        roots |= {_canonical_root(r[0]) for r in rows}
    except Exception as exc:  # noqa: BLE001
        raise RegistryUnreadable(str(exc)) from exc
    return roots - {""}


def _is_project_root(path: Path, registered: set[str]) -> bool:
    """The folder at ``path`` is ONE project AIDOCS governs: registered, or
    that exact folder commissioned / adopted. Any fault answers False."""
    if _canonical_root(path) in registered:
        return True
    try:
        from . import project_commission as _pc

        resolved = Path(path).resolve()
        return bool(_pc.is_commissioned(resolved) or _pc.adoption_evidence(resolved))
    except Exception:  # noqa: BLE001 -- unknown is never "a project"
        return False


def resolve_project(stated: object) -> tuple[str, list[str]]:
    """The ONE canonical project root a move grant is MINTED under (#1166
    blocker B, r0b1 77914355-7d9): ``(root, matching_registered_roots)``;
    root '' = mint nothing.

    An absolute path names its resolved root, and only when that root is a
    project (registered, or that exact folder commissioned / adopted -- a path
    names one folder, so it can never be ambiguous). A bare name names the ONE registered project whose
    folder has that name. Zero matches, two or more, or a registry that cannot
    be read mint nothing -- the operator did not say which project, so the
    agent never gets to pick one; the operator gives the path instead."""
    text = str(stated or "").strip().strip("`'\"“”‘’").rstrip(".,;:!?)").strip()
    if not text:
        return "", []
    try:
        registered = _registered_roots()
    except RegistryUnreadable:
        return "", []
    path = Path(text)
    if path.is_absolute():
        root = _canonical_root(path)
        return (root, [root]) if root and _is_project_root(path, registered) else ("", [])
    name = normalize_target(text)
    if "/" in name:
        return "", []
    matches = sorted(r for r in registered if normalize_target(Path(r).name) == name)
    return (matches[0], matches) if len(matches) == 1 else ("", matches)


def replace_binding_intents(
    host_session_id: str, intents: list[dict[str, str]], generation: int | None = None
) -> bool:
    """Make ``intents`` (``{"tool", "target"}`` dicts) the conversation's
    whole set of stated moves, stamped with this prompt's ``generation`` (from
    begin_prompt). True when written; False without a host id or generation,
    or on a store error (then no grant exists, so every move is refused).
    ``generation=None`` runs begin_prompt itself (direct callers / tests)."""
    host = str(host_session_id or "").strip()
    if not host:
        return False
    if generation is None:
        generation = begin_prompt(host)
        if generation is None:
            return False
    rows: set[tuple[str, str]] = set()
    for intent in intents or []:
        if not isinstance(intent, dict):
            continue
        tool = str(intent.get("tool") or "").strip()
        if tool in ("ai_session", SESSION_DENY_TOOL):
            # A session is named INSIDE a project; never stored bare. The
            # project is resolved to ONE canonical root (blocker B).
            target = session_key(resolve_project(intent.get("project"))[0], intent.get("target"))
        elif tool == "ai_project":
            target = resolve_project(intent.get("target"))[0]
        else:
            target = normalize_target(intent.get("target"))
        if tool and target:
            rows.add((tool, target))
    try:
        db_path = _global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db_path)
        try:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                f"SELECT generation FROM {GENERATION_TABLE} WHERE host_session_id = ?", (host,)
            ).fetchone()
            if current is None or int(current[0]) != int(generation):
                conn.rollback()
                return False  # a later prompt already began: this one mints nothing
            conn.execute(f"DELETE FROM {BINDING_INTENT_TABLE} WHERE host_session_id = ?", (host,))
            now = time.time()
            conn.executemany(
                f"INSERT OR REPLACE INTO {BINDING_INTENT_TABLE} "
                "(host_session_id, tool, target, minted_at, generation) VALUES (?, ?, ?, ?, ?)",
                [(host, tool, target, now, int(generation)) for tool, target in sorted(rows)],
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- no grant written is the fail-closed answer
        return False


def consume_binding_intent(host_session_id: str, tool: str, candidates: list[object]) -> str | None:
    """Spend the conversation's stated move for ``tool`` to ANY of
    ``candidates`` (the spellings of the one real target: full path, name).

    ONE statement reads and removes the row(s), so two concurrent acts cannot
    both spend one grant. Returns the spelling that matched, or None: no host
    id, no grant, expired, or an unreadable store -- the caller refuses."""
    host = str(host_session_id or "").strip()
    wanted = sorted({normalize_target(c) for c in candidates or [] if normalize_target(c)})
    if not host or not tool or not wanted:
        return None
    db_path = _global_db_path()
    if not db_path.is_file():
        return None
    marks = ", ".join("?" for _ in wanted)
    try:
        # The SAME lock begin_prompt holds (r0b1 77914355-7d9 A2): no revocation
        # can start or be half-done while this spend reads and deletes.
        with _host_lock(host) as fh:
            if _latch_pending(fh):
                return None  # a revocation is pending (blocker A): nothing is spent
            conn = _canonical_connect(db_path)
            try:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                if _latch_pending(fh):  # re-checked inside the serialized spend
                    conn.rollback()
                    return None
                current = conn.execute(
                    f"SELECT generation FROM {GENERATION_TABLE} WHERE host_session_id = ?", (host,)
                ).fetchone()
                if current is None:
                    conn.rollback()
                    return None
                rows = conn.execute(
                    f"DELETE FROM {BINDING_INTENT_TABLE} "
                    f"WHERE host_session_id = ? AND tool = ? AND target IN ({marks}) AND generation = ? "
                    "RETURNING target, minted_at",
                    (host, tool, *wanted, int(current[0])),
                ).fetchall()
                conn.commit()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001 -- lock, latch or store fault: nothing is spent
        return None
    fresh = [str(t) for t, at in rows if time.time() - float(at or 0.0) <= BINDING_INTENT_TTL_S]
    return fresh[0] if fresh else None


def binding_intent_held(host_session_id: str, tool: str, candidates: list[object]) -> bool:
    """True when a live grant for ``tool`` to any of ``candidates`` is held --
    read only, nothing spent. A store error reads as HELD (the caller uses it
    for exclusions, and an unreadable exclusion must still exclude)."""
    host = str(host_session_id or "").strip()
    wanted = sorted({normalize_target(c) for c in candidates or [] if normalize_target(c)})
    if not host or not tool or not wanted:
        return False
    db_path = _global_db_path()
    if not db_path.is_file():
        return False
    marks = ", ".join("?" for _ in wanted)
    try:
        conn = _canonical_connect(db_path)
        try:
            rows = conn.execute(
                f"SELECT minted_at FROM {BINDING_INTENT_TABLE} "
                f"WHERE host_session_id = ? AND tool = ? AND target IN ({marks})",
                (host, tool, *wanted),
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- see the docstring
        return True
    return any(time.time() - float(at or 0.0) <= BINDING_INTENT_TTL_S for (at,) in rows)


def _spend_session_move(host_session_id: str, roots: list[str], session_id: str) -> bool:
    """The operator named THIS session of THIS project, or ANY session of it
    without excluding this one. An exclusion wins over both: "connect to
    project x and any other session but y" never reaches x's y."""
    exact = [session_key(root, session_id) for root in roots]
    if binding_intent_held(host_session_id, SESSION_DENY_TOOL, exact):
        return False
    if consume_binding_intent(host_session_id, "ai_session", exact):
        return True
    anyof = [session_key(root, SESSION_ANY) for root in roots]
    return bool(consume_binding_intent(host_session_id, "ai_session", anyof))


def _spend_root(target: object) -> list[str]:
    """The one spelling that may SPEND a grant for the act's real target,
    re-resolved at the sink (r0b1 77914355-7d9 B): its canonical root, and only
    while that root is STILL a project (see _is_project_root) -- a registry
    change after the mint, or a registry that cannot be read now, spends
    nothing. Never a folder name."""
    text = str(target or "").strip()
    root = _canonical_root(text) if text else ""
    if not root:
        return []
    try:
        return [root] if _is_project_root(Path(text), _registered_roots()) else []
    except RegistryUnreadable:
        return []


def require_move_intent(
    host_session_id: str, tool: str, target: str, *, project_root: object = None
) -> dict[str, object] | None:
    """Spend the operator's stated move of THIS conversation to ``target``,
    or return the refusal. ``tool`` is ``ai_project`` (target = a project
    root) or ``ai_session`` (target = a session id IN ``project_root``, the
    project the connect binds in -- a grant for another project's session of
    the same name does not match). Factual refusal: it names the move and
    where consent comes from, and carries no token -- nothing the model could
    echo stands in for the operator's words."""
    target = str(target or "").strip()
    if tool == "ai_project":
        granted = bool(target) and bool(
            consume_binding_intent(host_session_id, tool, _spend_root(target))
        )
    else:
        roots = _spend_root(project_root) if project_root else []
        granted = bool(target and roots) and _spend_session_move(host_session_id, roots, target)
    if granted:
        return None
    noun = "project" if tool == "ai_project" else "session"
    shown = Path(target).name if tool == "ai_project" and target else target
    return {
        "bound": False,
        "connected": False,
        "_error": "human_intent_required",
        "action": f"{tool} connect",
        "target": target,
        "_detail": (
            f"{tool} connect to {target!r} was not performed: moving this "
            f"conversation to another {noun} changes the agent's scope, so it "
            f"needs the operator's own word in the current prompt (for "
            f"example: connect {noun} {shown}). No such request is held for "
            f"this conversation. A project is named by its full path, or by "
            f"its folder name only when exactly one registered project has "
            f"that name; if the name could mean more than one project, ask "
            f"the operator for the path."
        ),
    }
