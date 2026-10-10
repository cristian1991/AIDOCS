"""Pending ⚠ freeze-strike notice store + rail surfacing.

When a security strike is recorded (SecurityViolationService.record_and_escalate)
— including a self-cancel strike minted when the AGENT clears its own freeze
(clear_freeze_service) — a terse notice lands here and the universal
notification injector (``notification_injector._collect_notification_blocks``)
surfaces it on the agent's next tool calls.

Why a notification rail and NOT the UPS additional-context note it replaces:
the old hook_pipeline strike-note re-fired on EVERY prompt while peak>0 (a
per-prompt tax). This store surfaces a strike a bounded number of times
(``_MAX_SURFACES`` = 3, operator directive 2026-07-16) then auto-drops — the
agent is told, then it stops nagging. The blocked-tool error still renders the
full strike trail at block time (freeze_service._render_strike_trail); this rail
is the BETWEEN-blocks visibility that was previously missing.

Storage (WAR M Phase A, #445 — no-file-layer): rows live in the canonical
kingdom sqlite (``freeze_strike_notices`` table via ``SQLiteIndexStoreBase``),
NOT the legacy ``.MEMORY/.index/freeze_strike_notices.json`` loose file.
Heal-forward bridge mirrors the commission-stamp precedent
(mcp_server_runtime_helpers.stamp_commissioned): on the store's first touch a
still-present legacy JSON is adopted into the DB exactly once (stamped in
``index_meta``), then the DB is canonical — later edits to the file change
nothing, and NO new writes ever land in the file. The legacy file is NOT
deleted here (Phase B owns removal).

Mirrors aidocs_nlp/durable_hint_store surfaced_count/max_surfaces semantics.
Fail-quiet like every notification layer: any error enqueues/surfaces nothing
and breaks no caller.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import ProjectNotAdopted, SQLiteIndexStoreBase

__all__ = [
    "NOT_A_PROJECT",
    "NotAProject",
    "enqueue_strike_notice",
    "pending_for_session",
    "surface_pending",
    "format_strike_block",
]


class NotAProject(list):
    """The REFUSAL "this directory is not an AIDOCS project", as a value.

    #588 D6. Fail-quiet is the right policy for a notification rail and is
    NOT what was broken. What was broken is that fail-quiet was also
    fail-INDISTINGUISHABLE: the three module-level wrappers below caught
    ``Exception`` and returned a bare ``False`` / ``[]``, so a caller could
    not tell "your root is not adopted" (a refusal — the honest answer to
    the question it asked, per ``ProjectNotAdopted``'s own docstring) from
    "the write failed" from "sqlite is corrupt". That cost a ten-test
    diagnosis, because the real error was discarded three frames up.

    Shape, and why this one:
      * it is a ``list``, so it is FALSY, empty, iterable and ``== []`` —
        every existing caller (``if notices:``, ``for n in notices:``)
        keeps working, and the non-throwing contract is untouched;
      * it is a distinct TYPE and a singleton, so a caller that wants to
        know can ask — ``result is NOT_A_PROJECT`` or
        ``isinstance(result, NotAProject)`` — and get a different answer
        than it gets for a genuine write failure, which still returns the
        plain ``False`` / ``[]``.
    """

    __slots__ = ()


#: The single refusal instance. Identity comparison is the discriminator.
NOT_A_PROJECT = NotAProject()

_LEGACY_FILENAME = "freeze_strike_notices.json"
_ADOPTION_STAMP_KEY = "freeze_strike_notices_file_adopted"
_MAX_SURFACES = 3  # operator directive 2026-07-16: 3 surfaces, then auto-drop


def _conversation_key(row: Any) -> str:
    """The row's CONVERSATION actor key, or "" for a pre-#879 row.

    Tolerates the column being absent: an interrupted ALTER leaves the
    older shape, and a row with no conversation key must read as the
    actor-only row it is rather than raising on every drain.
    """
    try:
        return str(row["conversation_agent_context_id"] or "")
    except Exception:
        return ""


def _owned_by(row: Any, *, session_id: str, agent_context_id: str) -> bool:
    """Does this notice belong to the agent asking for it?

    ONE ownership rule, used by both ``surface`` and (opt-in) ``pending``
    so the two can never drift.

    Owner = the AGENT (agent_context_id), which FOLLOWS the agent across
    work sessions — agent_context_id derives from the agent's own
    host_session_id and EXCLUDES session_uuid (operator model
    2026-07-15). So when the notice carries an actor, the session need
    NOT match: the notice follows a moved agent.

    UNATTRIBUTED notices (#736 finding 4) used to fall back to SESSION
    ownership, so a notice minted with no actor — e.g. the hub-less
    self-cancel path in ``clear_freeze_service`` — was handed to EVERY
    other agent sharing the work session: a strike a bystander never
    earned. #588 D1's precedent (``UnattributableFreeze``) is that an
    actor-scoped fact with no resolvable actor must not bind a
    bystander. An actor-less notice is therefore delivered ONLY to an
    equally actor-less rail in its own session, never to an identified
    sibling.

    #879 B3 — THE CONVERSATION CLAUSE, and why it is not a fallback.
    Since 2026-08-22 a strike earned on the HOOK path is enqueued under
    the SUBAGENT's ``agent_context_id``. Every drain path is on the MCP
    transport, which cannot carry ``agent_id`` at all, so it resolves
    the CONVERSATION key and matched none of those rows. Combined with
    the unreachable prune below (the DELETE fires only at
    ``surfaced_count == _MAX_SURFACES``, and that only increments inside
    ``surface()``, which this very check already skipped) such a notice
    is never surfaced, so never counted, so NEVER DELETED — permanent.

    So the row also records the actor's OWN conversation key, and the
    owner of that conversation is an owner too. This substitutes nothing:
    both keys are RESOLVED and STORED, the actor's key stays exact and
    primary, and a conversation the actor does not belong to still
    matches nothing. A parent is not a bystander — it is the only actor
    the drain transport can name.

    KNOWN LIMIT, stated rather than hidden: a SIBLING subagent draining
    over MCP also resolves to that conversation key and will see the
    notice. That is the transport's inability to name it (#876), not a
    decision here; the strike LEDGER stays strictly per-subagent.
    """
    n_ctx = str(row["agent_context_id"] or "")
    if n_ctx:
        if n_ctx == agent_context_id:
            return True
        n_conv = _conversation_key(row)
        return bool(n_conv) and bool(agent_context_id) and n_conv == agent_context_id
    return not agent_context_id and str(row["session_id"] or "") == session_id


def _legacy_path(project_root: Path) -> Path:
    """Legacy loose-file location — read ONCE by the adoption bridge, never
    written. Retained (not deleted) until Phase B removal."""
    return Path(project_root) / ".MEMORY" / ".index" / _LEGACY_FILENAME


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FreezeStrikeNoticeStore(SQLiteIndexStoreBase):
    """Sqlite-backed freeze-strike notice rows (one row per strike)."""

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS freeze_strike_notices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                agent_context_id TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL DEFAULT 0,
                threshold INTEGER NOT NULL DEFAULT 0,
                family TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT '',
                surfaced_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                conversation_agent_context_id TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        # #879 B3 additive column. Purely additive (NOT NULL DEFAULT '') so no
        # table rebuild: an interrupted ALTER leaves the pre-#879 shape, which
        # every reader below still handles — a row with no conversation key is
        # read as the actor-only row it is, and binds exactly whom it bound.
        cols = {
            r[1] for r in conn.execute(
                "PRAGMA table_info(freeze_strike_notices)",
            ).fetchall()
        }
        if "conversation_agent_context_id" not in cols:
            conn.execute(
                "ALTER TABLE freeze_strike_notices ADD COLUMN "
                "conversation_agent_context_id TEXT NOT NULL DEFAULT ''",
            )

    def _adopt_legacy_file(self, conn: sqlite3.Connection, project_root: Path) -> None:
        """Heal-forward bridge — one-shot, stamped, file left in place.

        On the first DB touch: if the legacy JSON still exists, its pending
        notices are inserted; either way the adoption stamp is written so
        the DB is canonical from here on (a file that appears LATER is never
        folded back in — DB wins after adoption).
        """
        row = conn.execute(
            "SELECT 1 FROM index_meta WHERE key = ?",
            (_ADOPTION_STAMP_KEY,),
        ).fetchone()
        if row is not None:
            return
        legacy = _legacy_path(project_root)
        if legacy.is_file():
            try:
                raw = json.loads(legacy.read_text(encoding="utf-8"))
                pending = raw.get("pending") if isinstance(raw, dict) else None
            except Exception:
                pending = None
            for notice in pending if isinstance(pending, list) else []:
                if not isinstance(notice, dict):
                    continue
                session_id = str(notice.get("session_id") or "")
                if not session_id:
                    continue
                conn.execute(
                    "INSERT INTO freeze_strike_notices "
                    "(session_id, agent_context_id, count, threshold, family, "
                    " origin, surfaced_count, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        str(notice.get("agent_context_id") or ""),
                        int(notice.get("count") or 0),
                        int(notice.get("threshold") or 0),
                        str(notice.get("family") or ""),
                        str(notice.get("origin") or ""),
                        int(notice.get("surfaced_count") or 0),
                        str(notice.get("created_at") or _now()),
                    ),
                )
        conn.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
            (_ADOPTION_STAMP_KEY, _now()),
        )

    def _prepare(self, conn: sqlite3.Connection, project_root: Path) -> None:
        self._ensure_schema(conn)
        self._adopt_legacy_file(conn, project_root)

    @staticmethod
    def _row_to_notice(row: sqlite3.Row) -> dict[str, Any]:
        # Same dict shape the JSON store returned — no DB ``id`` leak, so
        # renderers/consumers see identical payloads.
        return {
            "session_id": row["session_id"],
            "agent_context_id": row["agent_context_id"],
            "count": int(row["count"]),
            "threshold": int(row["threshold"]),
            "family": row["family"],
            "origin": row["origin"],
            "surfaced_count": int(row["surfaced_count"]),
            "created_at": row["created_at"],
            "conversation_agent_context_id": _conversation_key(row),
        }

    def enqueue(
        self,
        project_root: Path,
        session_id: str,
        *,
        count: int,
        threshold: int,
        family: str = "",
        origin: str = "",
        agent_context_id: str = "",
        conversation_agent_context_id: str = "",
    ) -> bool:
        if not session_id:
            return False
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            conn.execute(
                "INSERT INTO freeze_strike_notices "
                "(session_id, agent_context_id, count, threshold, family, "
                " origin, surfaced_count, created_at, "
                " conversation_agent_context_id) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    session_id,
                    str(agent_context_id or ""),
                    int(count),
                    int(threshold),
                    str(family or ""),
                    str(origin or ""),
                    _now(),
                    str(conversation_agent_context_id or ""),
                ),
            )
        return True

    def pending(
        self,
        project_root: Path,
        session_id: str,
        agent_context_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pending notices for a session — INSPECTION api.

        ``agent_context_id=None`` (default) applies NO actor filter and
        returns everything labelled with the session, which is what the
        inspection/diagnostic callers want. Pass a string (``""``
        included) to apply the same ownership rule ``surface`` uses, so
        a caller that intends to SHOW these to an agent cannot hand a
        bystander someone else's strike (#736 finding 4).
        """
        if not session_id:
            return []
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            if agent_context_id is None:
                rows = conn.execute(
                    "SELECT * FROM freeze_strike_notices "
                    "WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            else:
                rows = [
                    r
                    for r in conn.execute(
                        "SELECT * FROM freeze_strike_notices ORDER BY id",
                    ).fetchall()
                    if _owned_by(
                        r, session_id=session_id,
                        agent_context_id=agent_context_id,
                    )
                ]
        return [self._row_to_notice(r) for r in rows]

    def surface(
        self,
        project_root: Path,
        *,
        session_id: str,
        agent_context_id: str = "",
        max_surfaces: int = _MAX_SURFACES,
    ) -> list[dict[str, Any]]:
        if not session_id:
            return []
        surfaced: list[dict[str, Any]] = []
        with self.session(project_root) as conn:
            self._prepare(conn, project_root)
            rows = conn.execute(
                "SELECT * FROM freeze_strike_notices ORDER BY id",
            ).fetchall()
            for row in rows:
                if not _owned_by(
                    row, session_id=session_id,
                    agent_context_id=agent_context_id,
                ):
                    continue
                new_count = int(row["surfaced_count"]) + 1
                notice = self._row_to_notice(row)
                notice["surfaced_count"] = new_count
                surfaced.append(notice)
                if new_count < max_surfaces:
                    conn.execute(
                        "UPDATE freeze_strike_notices SET surfaced_count = ? "
                        "WHERE id = ?",
                        (new_count, row["id"]),
                    )
                else:
                    # at/over cap: dropped from pending
                    conn.execute(
                        "DELETE FROM freeze_strike_notices WHERE id = ?",
                        (row["id"],),
                    )
        return surfaced


_STORE = FreezeStrikeNoticeStore()


def enqueue_strike_notice(
    project_root: Path,
    session_id: str,
    *,
    count: int,
    threshold: int,
    family: str = "",
    origin: str = "",
    agent_context_id: str = "",
    conversation_agent_context_id: str = "",
) -> bool | NotAProject:
    """Enqueue one pending ⚠ freeze-strike notice for ``session_id``.

    ``count``/``threshold`` drive the rendered ratchet ("N/3"); ``origin``
    ('self_cancel' | '') tailors the lesson text. One notice per strike
    (no dedup — each strike is a distinct event worth surfacing).

    Never raises (#588 D6 keeps that contract). Three distinguishable
    answers instead of two:
      * ``True``            — enqueued.
      * ``NOT_A_PROJECT``   — REFUSED: this root is not an AIDOCS project.
        Falsy, so no existing caller changes behaviour.
      * ``False``           — the write genuinely failed (sqlite error,
        corrupt store, anything else).
    """
    try:
        return _STORE.enqueue(
            project_root,
            session_id,
            count=count,
            threshold=threshold,
            family=family,
            origin=origin,
            agent_context_id=agent_context_id,
            conversation_agent_context_id=conversation_agent_context_id,
        )
    except ProjectNotAdopted:
        return NOT_A_PROJECT
    except Exception:
        return False


def pending_for_session(
    project_root: Path,
    session_id: str,
    agent_context_id: str | None = None,
) -> list[dict[str, Any]] | NotAProject:
    """Peek (no bump, no drop). Empty session_id returns [].

    ``agent_context_id=None`` (default) = inspection, no actor filter.
    Pass a string to apply ``surface``'s ownership rule (#736).

    Never raises. ``NOT_A_PROJECT`` (an empty, falsy list subclass) is the
    REFUSAL "not an AIDOCS project"; a plain ``[]`` still means "nothing
    pending, or the read failed" (#588 D6).
    """
    try:
        return _STORE.pending(project_root, session_id, agent_context_id)
    except ProjectNotAdopted:
        return NOT_A_PROJECT
    except Exception:
        return []


def surface_pending(
    project_root: Path,
    *,
    session_id: str,
    agent_context_id: str = "",
    max_surfaces: int = _MAX_SURFACES,
) -> list[dict[str, Any]] | NotAProject:
    """Peek + bump surfaced_count + auto-drop at ``max_surfaces``.

    Mirrors durable_hint_store.surface_pending: each call that RETURNS a
    notice counts as one surface; a notice that reaches the cap is dropped
    from pending. Empty session_id returns [].

    Never raises. Same three-way answer as ``pending_for_session`` (#588 D6).
    """
    try:
        return _STORE.surface(
            project_root,
            session_id=session_id,
            agent_context_id=agent_context_id,
            max_surfaces=max_surfaces,
        )
    except ProjectNotAdopted:
        return NOT_A_PROJECT
    except Exception:
        return []


def format_strike_block(notices: list[dict[str, Any]]) -> str:
    """Terse ⚠ rail block. One line per strike notice."""
    lines: list[str] = []
    for notice in notices:
        count = int(notice.get("count") or 0)
        threshold = int(notice.get("threshold") or 0)
        origin = str(notice.get("origin") or "")
        cause = (
            "self-cancelling your own freeze"
            if origin == "self_cancel"
            else "a repeated security denial"
        )
        ratchet = f"{count}/{threshold}" if threshold >= 1 else str(count)
        line = f"⚠ Freeze-strike {ratchet} — recorded for {cause}. "
        if threshold >= 1 and count >= threshold:
            line += (
                "You are AT the freeze ceiling — the next strike (or an active "
                "lock) freezes the session, clearable only by a genuine external "
                "operator. Stop and change approach."
            )
        elif threshold >= 1 and count >= threshold - 1:
            line += (
                "ONE more strike freezes the session (uncancelable — external "
                "operator only). Stop and rethink the approach instead of retrying."
            )
        else:
            line += "Avoid repeating the action that caused it."
        lines.append(line)
    return "\n".join(lines)

