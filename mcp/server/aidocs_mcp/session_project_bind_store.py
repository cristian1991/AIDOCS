"""Host-session → project bind store (install-wide, idle-TTL).

DOCTRINE (2026-05-31, Empire design): a persistent project bind is keyed by
``host_session_id``, NOT process-global. `ai_project(mode="bind")` records
"this host session is bound to project X"; `resolve_project_root()` consults
it (above cwd-discovery) so the bind sticks across tool calls even when CC
runs in a different directory. The bind carries an IDLE TTL (default 30 min,
dashboard-configurable). Since #1095 the refresh of ``last_activity`` is a
HOST-LIFECYCLE act, not a side effect of reading: the PreToolUse hook
refreshes it for every tool call on hosts with hooks, and on hosts without
hooks only tools NOT advertised read-only refresh it at the call boundary.
So on a hookless host a stretch of read-only calls longer than the TTL lets
the bind expire. Once idle past the TTL the bind is logically unbound
(evaluate() says EXPIRED) and resolution falls back to normal cwd-discovery;
the row itself is removed only by an explicit path (resolve / unbind /
rebind), never by a read.

WHY host-session keying = cross-user separation for free: each operator's
CC session has its own ``host_session_id``, so binds are physically isolated
— operator A binding their admin project can never re-root operator B's
session. Shared access is the RBAC layer's job (scoped grants in
``rbac_store``), gated at bind time by ``project_authority.require_cross_project``;
this store is pure binding state, never an authority of its own.

Lives in the same install-wide sqlite as KnownProjectsStore / ConfigStore
(``~/.aidocs/config.sqlite3``, overridable via ``AIDOCS_GLOBAL_CONFIG_DB``)
because the binding must be resolvable BEFORE the project is known — it
cannot live in a per-project DB (chicken-and-egg).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

# #755: canonical connect — WAL, synchronous=NORMAL, busy_timeout and
# foreign_keys=ON, none of which raw sqlite3.connect() applies. This
# schema declares no FOREIGN KEYs, so enabling enforcement is inert here.
# row_factory stays ROW (the helper's default): this store read by name
# already, so the hand-set line it replaces is redundant.
from ._sqlite_connect import connect as _canonical_connect

# Default idle TTL. Operator-overridable via the config key
# ``session.project_bind_ttl_minutes`` (dashboard); the store itself takes
# the resolved value as a parameter so it stays policy-free.
DEFAULT_BIND_TTL_MINUTES = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _parse(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


BIND_BOUND = "bound"
BIND_UNBOUND = "unbound"
BIND_EXPIRED = "expired"


@dataclass(frozen=True)
class BindState:
    """The judgement evaluate() returns. `project_root` is set for BOUND and
    EXPIRED (the row exists); only BOUND means the caller is on that root.

    `fingerprint` is the SEMANTIC VERSION of the judged row: every stored field
    the judgement depends on -- (project_root, raw last_activity_utc, raw
    ttl_minutes), where the TTL slot is `_NO_TTL_COLUMN` for a pre-ttl table.
    Every mutation derived from a BindState is a compare-and-set on it, so a
    stale judgement can never act on a row whose semantics changed underneath
    it -- a concurrent rebind, a refreshed stamp, or a TTL-only change
    (r0b2 0ec88c0b-ce2, 318e80cf-91e)."""

    status: str
    project_root: str | None = None
    last_activity: datetime | None = None
    fingerprint: tuple[str, str, object] | None = None


#: The TTL slot of a fingerprint judged against a table that has no
#: ttl_minutes column yet. A CAS against it misses once the column exists.
_NO_TTL_COLUMN = "__no_ttl_column__"


#: A CAS miss means the row changed between judgement and mutation; the
#: mutator re-judges the new row. Bounded so contention cannot spin forever.
_CAS_ATTEMPTS = 3


def ensure_bind_store_schema() -> None:
    """The bind store's schema + legacy migration, run at SERVER STARTUP.

    #1095 moved schema work off the read path: evaluate() never creates or
    migrates. The pre-ttl `ALTER TABLE ... ttl_minutes` migration therefore
    has a home here, on the boot rail (mcp_server.create_server), rather than
    riding the first read (r0b2 0ec88c0b-ce2). evaluate() still tolerates an
    unmigrated table, so a missed startup step degrades to the default TTL,
    never to an unresolvable bind."""
    SessionProjectBindStore().init_db()


class SessionProjectBindStore:
    """One row per host_session_id → bound project_root, with idle TTL."""

    def _global_db_path(self) -> Path:
        # Mirrors KnownProjectsStore/ConfigStore so all install-wide state
        # shares one DB + the same test override env var.
        override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
        if override:
            return Path(override)
        return Path.home() / ".aidocs" / "config.sqlite3"

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        db_path = self._global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db_path)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._session() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_project_binds (
                    host_session_id   TEXT PRIMARY KEY,
                    project_root      TEXT NOT NULL,
                    bound_by_uid      TEXT,
                    created_utc       TEXT NOT NULL,
                    last_activity_utc TEXT NOT NULL,
                    ttl_minutes       INTEGER NOT NULL DEFAULT 30
                );
                """,
            )
            # Migration: a row from before the ttl_minutes column existed.
            # ADD COLUMN is a no-op-or-error depending on prior state; swallow.
            try:
                conn.execute(
                    "ALTER TABLE session_project_binds ADD COLUMN "
                    "ttl_minutes INTEGER NOT NULL DEFAULT 30",
                )
            except sqlite3.OperationalError:
                pass  # column already present

    # ── mutate ────────────────────────────────────────────────────────

    def bind(
        self,
        host_session_id: str,
        project_root: Path | str,
        *,
        bound_by_uid: str = "",
        ttl_minutes: int = DEFAULT_BIND_TTL_MINUTES,
        now: datetime | None = None,
    ) -> None:
        """Bind (or rebind) a host session to a project. Idempotent —
        re-binding the same session updates the root + refreshes activity.
        The configured ttl_minutes is captured INTO the row so resolve()
        honors the value that was in effect at bind time (dashboard-tunable
        via session.project_bind_ttl_minutes).
        """
        sid = (host_session_id or "").strip()
        if not sid:
            raise ValueError("host_session_id is required to bind")
        root = str(Path(project_root)).strip()
        if not root:
            raise ValueError("project_root is required to bind")
        ts = _iso(now or _utcnow())
        ttl = int(ttl_minutes) if ttl_minutes is not None else DEFAULT_BIND_TTL_MINUTES
        self.init_db()
        with self._session() as conn:
            conn.execute(
                """
                INSERT INTO session_project_binds
                    (host_session_id, project_root, bound_by_uid, created_utc,
                     last_activity_utc, ttl_minutes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_session_id) DO UPDATE SET
                    project_root = excluded.project_root,
                    bound_by_uid = excluded.bound_by_uid,
                    last_activity_utc = excluded.last_activity_utc,
                    ttl_minutes = excluded.ttl_minutes
                """,
                (sid, root, bound_by_uid, ts, ts, ttl),
            )

    def touch(self, host_session_id: str, *, now: datetime | None = None) -> bool:
        """Compatibility name for touch_live() -- ONE meaning of "touch".

        This used to be an unconditional UPDATE by host_session_id: no TTL
        judgement, no compare-and-set, so it could revive an EXPIRED bind and
        contradicted touch_live() (r0b2 318e80cf-91e). It now delegates:
        refresh a LIVE bind only; False for no bind or an expired one."""
        return self.touch_live(host_session_id, now=now)

    def unbind(self, host_session_id: str) -> bool:
        """Remove a bind. Returns True if a row was deleted."""
        sid = (host_session_id or "").strip()
        if not sid:
            return False
        self.init_db()
        with self._session() as conn:
            cur = conn.execute(
                "DELETE FROM session_project_binds WHERE host_session_id = ?",
                (sid,),
            )
            return cur.rowcount > 0

    # ── evaluate: THE bind-state judgement, pure ─────────────────────

    def evaluate(
        self,
        host_session_id: str,
        *,
        ttl_minutes: int = DEFAULT_BIND_TTL_MINUTES,
        now: datetime | None = None,
    ) -> BindState:
        """What is this host session's bind state RIGHT NOW? Pure (#1095).

        BOUND (live, carries the root) / EXPIRED (a row exists but idled past
        its TTL, or its stamp is unreadable) / UNBOUND (no host session, no
        store, no table, no row). No UPDATE, no DELETE, and no database file
        is created where none exists -- asking must not change the answer or
        the store. This is the ONE place the TTL rule lives: resolve() and
        touch_live() judge through it rather than re-deriving it.
        """
        sid = (host_session_id or "").strip()
        if not sid:
            return BindState(BIND_UNBOUND)
        db_path = self._global_db_path()
        if not db_path.is_file():
            return BindState(BIND_UNBOUND)
        # READ-ONLY OPEN: purity enforced by the connection, not only by DML
        # discipline -- and no persistent journal_mode write (r0b2 hygiene).
        conn = _canonical_connect(db_path, read_only=True)
        try:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_project_binds'"
            ).fetchone()
            if present is None:
                return BindState(BIND_UNBOUND)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(session_project_binds)").fetchall()}
            ttl_col = "ttl_minutes" if "ttl_minutes" in columns else "NULL AS ttl_minutes"
            row = conn.execute(
                f"SELECT project_root, last_activity_utc, {ttl_col} "  # noqa: S608 -- fixed identifiers
                "FROM session_project_binds WHERE host_session_id = ?",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return BindState(BIND_UNBOUND)
        row_ttl = row["ttl_minutes"]
        effective_ttl = int(row_ttl) if row_ttl is not None else ttl_minutes
        raw_last = str(row["last_activity_utc"])
        last = _parse(raw_last)
        root = str(row["project_root"])
        fingerprint = (root, raw_last, row_ttl if "ttl_minutes" in columns else _NO_TTL_COLUMN)
        now_dt = now or _utcnow()
        if last is None or now_dt - last > timedelta(minutes=max(0, effective_ttl)):
            return BindState(BIND_EXPIRED, project_root=root, last_activity=last, fingerprint=fingerprint)
        return BindState(BIND_BOUND, project_root=root, last_activity=last, fingerprint=fingerprint)

    # ── resolve: the EXPLICIT mutating path (cleanup + optional keepalive) ──

    def resolve(
        self,
        host_session_id: str,
        *,
        ttl_minutes: int = DEFAULT_BIND_TTL_MINUTES,
        now: datetime | None = None,
        refresh: bool = False,
        refresh_throttle_seconds: int = 60,
    ) -> str | None:
        """The bound project_root, or None -- WITH side effects, for explicit
        callers only (bind/status service, maintenance). An expired bind is
        deleted; with refresh=True a live bind's last_activity is bumped
        (throttled). Tool-root resolution must NOT use this: it reads through
        evaluate(), which writes nothing (#1095).

        Judges through evaluate() (one TTL authority) and mutates ONLY the
        exact row judged: every DELETE/UPDATE is a compare-and-set on the
        judgement's fingerprint, and a miss re-judges the row that replaced it.
        """
        now_dt = now or _utcnow()
        sid = (host_session_id or "").strip()
        for _ in range(_CAS_ATTEMPTS):
            state = self.evaluate(host_session_id, ttl_minutes=ttl_minutes, now=now_dt)
            if state.status == BIND_EXPIRED:
                if self._cas(sid, state.fingerprint, delete=True):
                    return None
                continue  # the judged row changed: re-judge whatever replaced it
            if state.status != BIND_BOUND:
                return None
            if not refresh or not self._stale(state, now_dt, refresh_throttle_seconds):
                return state.project_root
            if self._cas(sid, state.fingerprint, stamp=_iso(now_dt)):
                return state.project_root
        # RETRIES EXHAUSTED (r0b2 318e80cf-91e): the last judgement is KNOWN
        # stale -- its CAS just missed. Answer only from a fresh judgement of
        # what is there now, never from the disproved one.
        final = self.evaluate(host_session_id, ttl_minutes=ttl_minutes, now=now_dt)
        return final.project_root if final.status == BIND_BOUND else None

    def touch_live(
        self,
        host_session_id: str,
        *,
        now: datetime | None = None,
        throttle_seconds: int = 60,
    ) -> bool:
        """KEEPALIVE of a LIVE bind -- the host-lifecycle refresh (#1095).

        Refreshes last_activity only when evaluate() says BOUND, throttled so a
        burst of calls writes once. An EXPIRED bind is never revived here:
        expiry is the documented un-bind, and bringing a lapsed routing lease
        back to life is a bind decision, not a heartbeat. The UPDATE is a
        compare-and-set on the judged row's semantic version; on a miss the
        heartbeat re-judges the row that replaced it and touches it only if
        THAT row is live. Returns True iff the bind is live.
        """
        now_dt = now or _utcnow()
        sid = (host_session_id or "").strip()
        for _ in range(_CAS_ATTEMPTS):
            state = self.evaluate(host_session_id, now=now_dt)
            if state.status != BIND_BOUND:
                return False
            if not self._stale(state, now_dt, throttle_seconds):
                return True
            if self._cas(sid, state.fingerprint, stamp=_iso(now_dt)):
                return True
        return self.evaluate(host_session_id, now=now_dt).status == BIND_BOUND

    @staticmethod
    def _stale(state: BindState, now_dt: datetime, throttle_seconds: int) -> bool:
        last = state.last_activity
        return last is None or (now_dt - last).total_seconds() > max(0, throttle_seconds)

    def _cas(
        self,
        sid: str,
        fingerprint: tuple[str, str, object] | None,
        *,
        delete: bool = False,
        stamp: str = "",
    ) -> bool:
        """ONE conditional mutation of the judged row: DELETE it, or set its
        last_activity to `stamp`. True iff the row still carries the exact
        semantic version that was judged. Takes the write lock FIRST, so the
        schema check and the conditional write see one state: a TTL column
        that appeared since the judgement (pre-ttl migration) is a miss."""
        if fingerprint is None:
            return False
        root, raw_last, raw_ttl = fingerprint
        with self._session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            has_ttl = "ttl_minutes" in {
                r[1] for r in conn.execute("PRAGMA table_info(session_project_binds)").fetchall()
            }
            if has_ttl == (raw_ttl == _NO_TTL_COLUMN):
                return False  # the schema changed under the judgement
            where = "host_session_id = ? AND project_root = ? AND last_activity_utc = ?"
            params: list[object] = [sid, root, raw_last]
            if has_ttl:
                where += " AND ttl_minutes IS ?"
                params.append(raw_ttl)
            if delete:
                sql = f"DELETE FROM session_project_binds WHERE {where}"  # noqa: S608 -- fixed clauses
                return conn.execute(sql, params).rowcount > 0
            sql = f"UPDATE session_project_binds SET last_activity_utc = ? WHERE {where}"  # noqa: S608
            return conn.execute(sql, [stamp, *params]).rowcount > 0

    def list_binds(self) -> list[dict[str, str]]:
        self.init_db()
        with self._session() as conn:
            rows = conn.execute(
                "SELECT host_session_id, project_root, bound_by_uid, created_utc, "
                "last_activity_utc FROM session_project_binds ORDER BY last_activity_utc DESC",
            ).fetchall()
        return [dict(r) for r in rows]
