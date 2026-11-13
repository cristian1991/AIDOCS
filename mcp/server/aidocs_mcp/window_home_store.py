"""``window -> HOME project``, pinned at the window's first SessionStart (#1166 B7').

WHY IT EXISTS. Every hook event used to resolve its project from the payload's
``cwd``. A ``cd`` into another commissioned tree (a git worktree of the same
repo, measured 2026-10-13) therefore re-rooted the whole window: the hooks found
no managed session there and refused every tool, ``ai_msg`` included, while the
window's lease and binding were intact on the project it started in.

The window's #880 lease cannot answer this on its own: it lives INSIDE each
project's store (``WindowBindingStore``), so a hook has to know the project
before it can read it. This is the install-wide locator that comes first --
same DB as ``SessionProjectBindStore`` / ``KnownProjectsStore``
(``~/.aidocs/config.sqlite3``, ``AIDOCS_GLOBAL_CONFIG_DB`` in tests).

WRITE ONCE PER WINDOW. The window key is ``<claude.exe pid>:<creation
filetime>`` (``window_key.py``), so it names one host process for its whole
life and is never reused. The first SessionStart attested in a commissioned
project pins it; later SessionStarts in the same window (``/clear``,
``/resume``, compact) -- possibly from wherever the shell had moved to -- never
rewrite it (r0b1 ruling on 8082261d-427: HOME stays pinned; re-homing is a
deliberate, consented act, not a side effect of a directory).

NOT AN AUTHORITY. This answers "which project does this window's hook serve";
identity is still the lease in that project, and permission is still RBAC.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect

WINDOW_HOME_TABLE = "window_home"


def _global_db_path() -> Path:
    # Mirrors SessionProjectBindStore / KnownProjectsStore: one install-wide DB
    # and the same test override.
    override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "config.sqlite3"


def _valid_window(window_key: str) -> str:
    from .window_key import WINDOW_KEY_SHAPE

    window = str(window_key or "").strip()
    return window if WINDOW_KEY_SHAPE.fullmatch(window) else ""


def pin_window_home(window_key: str, home_root: Path | str, *, host_session_id: str = "") -> bool:
    """Pin ``window_key``'s HOME to ``home_root`` unless it is already pinned.

    Returns True only when THIS call wrote the pin. Never raises: SessionStart
    availability is not negotiable (same posture as record_session_start_window).
    """
    window = _valid_window(window_key)
    root = str(Path(home_root)).strip() if home_root else ""
    if not window or not root:
        return False
    try:
        db_path = _global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db_path)
        try:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {WINDOW_HOME_TABLE} (
                    window_key            TEXT PRIMARY KEY,
                    home_root             TEXT NOT NULL,
                    pinned_host_session_id TEXT NOT NULL DEFAULT '',
                    pinned_at             TEXT NOT NULL,
                    source                TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur = conn.execute(
                f"INSERT OR IGNORE INTO {WINDOW_HOME_TABLE} "
                "(window_key, home_root, pinned_host_session_id, pinned_at, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    window,
                    root,
                    str(host_session_id or "").strip(),
                    datetime.now(UTC).isoformat(),
                    "session_start",
                ),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- a pin that cannot be written is "no pin"
        return False


class WindowHomeUnreadable(RuntimeError):
    """The pin store exists but could not be read: whether this window has a
    HOME is UNKNOWN. Never collapsed into "no pin" (r0b1 a8601d0e-612): that
    would silently move the window onto its cwd."""


def window_home(window_key: str) -> Path | None:
    """The pinned HOME for ``window_key``, or None when there is PROVABLY no pin
    (no valid key, no store file, no table yet, no row). A pure read on the hook
    hot path: no schema creation, no directory creation. Any other read failure
    raises ``WindowHomeUnreadable`` -- unknown is not absent."""
    window = _valid_window(window_key)
    if not window:
        return None
    db_path = _global_db_path()
    if not db_path.is_file():
        return None
    try:
        conn = _canonical_connect(db_path)
        try:
            row = conn.execute(
                f"SELECT home_root FROM {WINDOW_HOME_TABLE} WHERE window_key = ?",
                (window,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None  # no window has been pinned on this box yet
        raise WindowHomeUnreadable(f"{type(exc).__name__}: {exc}") from None
    except Exception as exc:  # noqa: BLE001 -- reported, never read as "no pin"
        raise WindowHomeUnreadable(f"{type(exc).__name__}: {exc}") from None
    if row is None:
        return None
    home = str(row[0] or "").strip()
    return Path(home) if home else None
