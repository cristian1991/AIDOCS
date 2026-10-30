"""Session anchor stack — lacunary speak fallback.

Phase 6 (2026-05-15). When the user says "fix that bug" or "edit it" with
no clear anchor in the current prompt, look back in a per-session ring
buffer of the last 10 distinct nouns and resolve the demonstrative.

Storage: session_anchor_stack table (kingdom DB). FIFO eviction at 10.
NEVER promoted to persistent memory — pure session-scoped RAM-equivalent.

This is the EDGE-CASE path. The main path is resolution-against-index in
anchor_field.extract_field. When that produces no anchor and the prompt
has a demonstrative, this stack fills the gap.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# #755: canonical connect (pragmas). row_factory=False preserves the plain
# tuples raw sqlite3.connect() returned -- nothing here reads by name.
from ._sqlite_connect import connect as _canonical_connect

_STACK_CAP = 10

_DEMONSTRATIVE_TOKENS = frozenset(
    {
        "that",
        "this",
        "it",
        "those",
        "these",
        "such",
        "same",
        "above",
    },
)

_DEMONSTRATIVE_RE = re.compile(
    r"\b(?:that|this|it|those|these)\s+\w+\b",
    re.IGNORECASE,
)


@dataclass
class AnchorRow:
    session_id: str
    turn_index: int
    noun: str
    lemma: str
    anchor_node_id: str = ""


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "aidocs.sqlite3"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_anchor_stack (
            session_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            noun TEXT NOT NULL,
            lemma TEXT NOT NULL,
            anchor_node_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, turn_index, noun)
        )
        """,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_anchor_stack_session "
        "ON session_anchor_stack(session_id, turn_index DESC)",
    )


def push(
    project_root: Path,
    session_id: str,
    nouns: list[str],
    *,
    lemmas: list[str] | None = None,
) -> int:
    """Push a turn's nouns onto the stack. FIFO-evicts the oldest turns
    when total entries exceed _STACK_CAP. Returns the new turn_index.
    """
    if not nouns:
        return -1
    lemmas_resolved = list(lemmas or nouns)
    db = _db_path(project_root)
    if not db.parent.is_dir():
        return -1
    try:
        conn = _canonical_connect(str(db), row_factory=False)
        try:
            _ensure_table(conn)
            # Determine next turn_index per session.
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) + 1 "
                "FROM session_anchor_stack WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            next_turn = int(row[0]) if row else 0
            # Insert this turn's nouns.
            for noun, lemma in zip(nouns, lemmas_resolved):
                n = (noun or "").strip().lower()
                l = (lemma or n or "").strip().lower()
                if not n:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO session_anchor_stack "
                    "(session_id, turn_index, noun, lemma) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, next_turn, n, l),
                )
            # FIFO eviction: keep only the latest _STACK_CAP distinct
            # turn_indexes per session.
            keep_rows = conn.execute(
                "SELECT DISTINCT turn_index FROM session_anchor_stack "
                "WHERE session_id = ? ORDER BY turn_index DESC LIMIT ?",
                (session_id, _STACK_CAP),
            ).fetchall()
            if keep_rows:
                min_keep = int(keep_rows[-1][0])
                conn.execute(
                    "DELETE FROM session_anchor_stack WHERE session_id = ? AND turn_index < ?",
                    (session_id, min_keep),
                )
            conn.commit()
            return next_turn
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def get(
    project_root: Path,
    session_id: str,
    *,
    limit: int = _STACK_CAP,
) -> list[AnchorRow]:
    """Read the stack newest-first."""
    db = _db_path(project_root)
    if not db.is_file():
        return []
    try:
        conn = _canonical_connect(str(db), row_factory=False)
        try:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT session_id, turn_index, noun, lemma, anchor_node_id "
                "FROM session_anchor_stack "
                "WHERE session_id = ? "
                "ORDER BY turn_index DESC, noun ASC "
                "LIMIT ?",
                (session_id, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [
        AnchorRow(
            session_id=str(r[0]),
            turn_index=int(r[1]),
            noun=str(r[2]),
            lemma=str(r[3]),
            anchor_node_id=str(r[4] or ""),
        )
        for r in rows
    ]


def has_demonstrative(text: str) -> bool:
    """True when the prompt contains a demonstrative ('that bug', 'it',
    'this thing').
    """
    if not text:
        return False
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    return any(t in _DEMONSTRATIVE_TOKENS for t in tokens)


def resolve_demonstrative(
    project_root: Path,
    session_id: str,
    *,
    current_anchors: list[str] | None = None,
    limit: int = _STACK_CAP,
) -> list[str]:
    """Return nouns from the anchor stack that could resolve a
    demonstrative in the current prompt.

    Excludes nouns already in `current_anchors`. Returns newest first,
    deduped. Caller decides whether to backfill into KingField.anchors.
    """
    current = set(s.lower() for s in (current_anchors or []))
    out: list[str] = []
    seen: set[str] = set()
    for row in get(project_root, session_id, limit=limit):
        n = row.noun.lower()
        if n in current or n in seen:
            continue
        seen.add(n)
        out.append(row.noun)
    return out


def clear(project_root: Path, session_id: str) -> int:
    """Drop the entire stack for a session. Returns number of rows
    removed.
    """
    db = _db_path(project_root)
    if not db.is_file():
        return 0
    try:
        conn = _canonical_connect(str(db), row_factory=False)
        try:
            _ensure_table(conn)
            cur = conn.execute(
                "DELETE FROM session_anchor_stack WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            return cur.rowcount or 0
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
