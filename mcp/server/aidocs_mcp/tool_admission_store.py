"""``(conversation, tool_use_id) -> the project whose rules admitted it`` (#1166).

WHY IT EXISTS (r0b1 7728434a-73d, blocker 3). After a move to B, B's rules
judge a tool at PreToolUse (operator 2026-10-17). Its PostToolUse /
PostToolUseFailure then records the outcome -- the universal audit row, the
native Bash receipt and output guard, post-read redaction, edit witnesses --
and must record it under THAT project. Re-resolving the selection at Post is
wrong when it changed in between (B -> C, an expired bind, an unreadable
store): B's outcome would be written into C, or into HOME.

So a PreToolUse that ADMITS a call (any verdict but deny) writes one row and
the Post TAKES it (read + delete). PROVENANCE (r0b1 60f49c3a-505 blocker B):
the key is the conversation's ``host_session_id`` AND the host's
``tool_use_id`` -- both host-stamped by Claude Code, never a model argument --
so another window's Post cannot consume or reroute this one's row, and a
missing id is never treated as proof.

Same install-wide DB as ``window_home_store`` / ``SessionProjectBindStore``
(``~/.aidocs/config.sqlite3``, ``AIDOCS_GLOBAL_CONFIG_DB`` in tests). Rows whose
Post never came (a killed host) are pruned at SessionStart, not on the
per-call path.

NOT AN AUTHORITY: it records which project's rules ALREADY admitted the call;
it never admits anything itself.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ._sqlite_connect import connect as _canonical_connect

TOOL_ADMISSION_TABLE = "tool_admission"

#: A row older than this belongs to an invocation whose Post never came.
ADMISSION_TTL_S = 6 * 60 * 60

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TOOL_ADMISSION_TABLE} (
    host_session_id TEXT NOT NULL,
    tool_use_id     TEXT NOT NULL,
    project_root    TEXT NOT NULL,
    admitted_at     REAL NOT NULL,
    PRIMARY KEY (host_session_id, tool_use_id)
)
"""


def _global_db_path() -> Path:
    override = os.environ.get("AIDOCS_GLOBAL_CONFIG_DB", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".aidocs" / "config.sqlite3"


def _key(host_session_id: str, tool_use_id: str) -> tuple[str, str] | None:
    sid = str(host_session_id or "").strip()
    tid = str(tool_use_id or "").strip()
    return (sid, tid) if sid and tid else None


def record_admission(tool_use_id: str, project_root: Path | str, *, host_session_id: str) -> bool:
    """Record that ``project_root``'s rules admitted this conversation's call.

    Returns True when the row was written; False without both ids (nothing to
    prove the Post by) or on a store error. Never raises: the Pre verdict must
    not depend on this write -- its Post then finds no row and fails safe."""
    key = _key(host_session_id, tool_use_id)
    root = str(Path(project_root)).strip() if project_root else ""
    if key is None or not root:
        return False
    try:
        db_path = _global_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _canonical_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.execute(
                f"INSERT OR REPLACE INTO {TOOL_ADMISSION_TABLE} "
                "(host_session_id, tool_use_id, project_root, admitted_at) VALUES (?, ?, ?, ?)",
                (*key, root, time.time()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- see the docstring
        return False


def take_admission(tool_use_id: str, *, host_session_id: str) -> Path | None:
    """Pop the project that admitted THIS conversation's ``tool_use_id``.

    None when it is not provable: an id missing, no row (never admitted,
    already taken, another conversation's), expired, or an unreadable store.
    The caller must not substitute another project for it."""
    key = _key(host_session_id, tool_use_id)
    if key is None:
        return None
    db_path = _global_db_path()
    if not db_path.is_file():
        return None
    try:
        conn = _canonical_connect(db_path)
        try:
            # ONE statement reads and removes the row (r0b1 b4b5110c-526): a
            # SELECT then DELETE let two concurrent Posts both observe it
            # before either removed it, proving one admission twice.
            row = conn.execute(
                f"DELETE FROM {TOOL_ADMISSION_TABLE} "
                "WHERE host_session_id = ? AND tool_use_id = ? "
                "RETURNING project_root, admitted_at",
                key,
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- "no table yet" included: nothing to take
        return None
    if row is None:
        return None
    root, admitted_at = str(row[0] or "").strip(), float(row[1] or 0.0)
    if not root or time.time() - admitted_at > ADMISSION_TTL_S:
        return None
    return Path(root)


def prune_stale_admissions() -> int:
    """Delete rows older than ``ADMISSION_TTL_S``. Called on the SessionStart
    lifecycle rail, never per tool call. Never raises; returns rows removed."""
    db_path = _global_db_path()
    if not db_path.is_file():
        return 0
    try:
        conn = _canonical_connect(db_path)
        try:
            cur = conn.execute(
                f"DELETE FROM {TOOL_ADMISSION_TABLE} WHERE admitted_at < ?",
                (time.time() - ADMISSION_TTL_S,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- "no table yet" included
        return 0
