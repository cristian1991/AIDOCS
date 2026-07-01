"""Host-session → project bind store (install-wide, idle-TTL).

DOCTRINE (2026-05-31, king design): a persistent project bind is keyed by
``host_session_id``, NOT process-global. `ai_project(mode="bind")` records
"this host session is bound to project X"; `resolve_project_root()` consults
it (above cwd-discovery) so the bind sticks across tool calls even when CC
runs in a different directory. The bind carries an IDLE TTL (default 30 min,
dashboard-configurable): any tool call / UPS refreshes ``last_activity``;
once idle past the TTL the bind expires and resolution falls back to normal
cwd-discovery.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
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
        """Refresh last_activity for a live bind. Returns True if a row was
        refreshed, False if there is no bind for this session."""
        sid = (host_session_id or "").strip()
        if not sid:
            return False
        ts = _iso(now or _utcnow())
        self.init_db()
        with self._session() as conn:
            cur = conn.execute(
                "UPDATE session_project_binds SET last_activity_utc = ? WHERE host_session_id = ?",
                (ts, sid),
            )
            return cur.rowcount > 0

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

    # ── resolve ───────────────────────────────────────────────────────

    def resolve(
        self,
        host_session_id: str,
        *,
        ttl_minutes: int = DEFAULT_BIND_TTL_MINUTES,
        now: datetime | None = None,
        refresh: bool = False,
        refresh_throttle_seconds: int = 60,
    ) -> str | None:
        """Return the bound project_root for this host session, or None when
        there is no bind OR it has gone idle past ttl_minutes. An expired
        bind is deleted lazily on read (fail-safe: a stale bind never
        silently re-roots).

        When refresh=True and the bind is live, last_activity is bumped —
        this is the idle-TTL keepalive driven from resolve_project_root().
        The write is THROTTLED (refresh_throttle_seconds, default 60s): on
        the hot path resolve runs many times per tool call, so we only
        actually write once the existing stamp is older than the throttle.
        That keeps an active bind alive without per-call DB churn.
        """
        sid = (host_session_id or "").strip()
        if not sid:
            return None
        self.init_db()
        now_dt = now or _utcnow()
        with self._session() as conn:
            row = conn.execute(
                "SELECT project_root, last_activity_utc, ttl_minutes "
                "FROM session_project_binds WHERE host_session_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                return None
            # The TTL stored AT BIND TIME wins (dashboard-tunable); the param
            # is only a fallback for rows that predate the column.
            row_ttl = row["ttl_minutes"]
            effective_ttl = int(row_ttl) if row_ttl is not None else ttl_minutes
            last = _parse(str(row["last_activity_utc"]))
            if last is None or now_dt - last > timedelta(minutes=max(0, effective_ttl)):
                # Expired (or unparseable timestamp) → drop it, fall back.
                conn.execute(
                    "DELETE FROM session_project_binds WHERE host_session_id = ?",
                    (sid,),
                )
                return None
            if refresh and (now_dt - last).total_seconds() > max(0, refresh_throttle_seconds):
                conn.execute(
                    "UPDATE session_project_binds SET last_activity_utc = ? "
                    "WHERE host_session_id = ?",
                    (_iso(now_dt), sid),
                )
            return str(row["project_root"])

    def list_binds(self) -> list[dict[str, str]]:
        self.init_db()
        with self._session() as conn:
            rows = conn.execute(
                "SELECT host_session_id, project_root, bound_by_uid, created_utc, "
                "last_activity_utc FROM session_project_binds ORDER BY last_activity_utc DESC",
            ).fetchall()
        return [dict(r) for r in rows]
