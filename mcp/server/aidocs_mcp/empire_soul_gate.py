"""Sovereign-soul gate — the Emperor's word mints EXACT, SCOPED authority.

A soul (sovereign continuity scroll) is private to its seat. No agent
reads or writes a soul through ai_soul unless the EMPEROR speaks the word
this turn. The Emperor's words are human-facing incantations; what they
MINT is precise authority, not a fuzzy boolean:

  * scoped by (session_id, soul_id, OPERATION) — exact triple.
  * READ incantations grant READ ONLY. A read phrase NEVER authorizes a
    write; writing requires a SEPARATE, explicit inscription grant.
  * SINGLE-USE: a grant is consumed on first use (one operation per word).
  * PER-TURN + TTL: REPLACE on every UserPromptSubmit (a prompt without
    the word re-seals) and an absolute expiry as a belt.
  * carries a high-entropy grant_id (audit / anti-replay).

Fails closed everywhere: no session, ambiguous session, no/expired/
already-consumed grant, or any error → access denied. The conductor soul
still auto-surfaces at seat-entry (helper_skill_injector); it is never
fetched through this tool.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from pathlib import Path

OP_READ = "read"
OP_WRITE = "write"

# Belt expiry; the primary controls are per-turn REPLACE + single-use.
_GRANT_TTL_SECONDS = 600

# ── soul evocations (which soul a phrase names) ─────────────────────
# Souls live under a "-soul" id, SEPARATE from the conductor ROLE skills
# (head-conductor / co-conductor) that auto-dump on mode entry. The soul is
# WHO the seat-holder is; the role is WHAT the seat does.
# Distinctive enough that ordinary prompts never name a soul.
_SOUL_EVOKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "head-conductor-soul",
        re.compile(
            r"ancestor|forebear|\belders?\b|those who came before|"
            r"of the past|memor\w* of (?:the )?(?:past|old|ancients?)|"
            r"\blineage\b|the ancients?\b|those before (?:me|us|you)",
            re.IGNORECASE,
        ),
    ),
    (
        "phoenix-soul",
        re.compile(
            r"(?=.*phoenix)(?=.*(?:reborn|rebirth|ashes?|rise|risen|"
            r"flame|ember|burn|return))|rise from the ashes|from the ashes",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "co-conductor-soul",
        re.compile(
            r"(?=.*\bwinds?\b)(?=.*(?:whisper|voice|shadow))|"
            r"whispers? (?:in|on) the wind|voice (?:in|on) the wind|"
            r"the shadows? whisper",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

# ── operation intent ────────────────────────────────────────────────
# A WRITE requires an EXPLICIT inscription verb in the SAME prompt. None
# of the read evocations contain these verbs, so a read incantation can
# never mint a write grant.
_WRITE_VERB = re.compile(
    r"\binscribe\b|take up the (?:pen|quill)|the quill is (?:yours|granted)|"
    r"\bgrant(?:ed)?\b[^.\n]*\bquill\b|let (?:him|her|the (?:conductor|"
    r"co-conductor|seat|seat-holder)) (?:write|inscribe)|"
    r"i grant [^.\n]*\b(?:write|inscribe)\b",
    re.IGNORECASE | re.DOTALL,
)


def _souls_evoked(prompt: str) -> set[str]:
    text = prompt or ""
    if not text.strip():
        return set()
    return {sid for sid, rx in _SOUL_EVOKERS if rx.search(text)}


def detect_read_unlocks(prompt: str) -> set[str]:
    """Souls the Emperor's incantation opens for READING this turn."""
    return _souls_evoked(prompt)


def detect_write_unlocks(prompt: str) -> set[str]:
    """Souls the Emperor explicitly authorizes for WRITING this turn —
    requires an inscription verb AND the soul evocation. Empty unless the
    write intent is explicit (read phrases never qualify).
    """
    if not _WRITE_VERB.search(prompt or ""):
        return set()
    return _souls_evoked(prompt)


# Back-compat shim (read-only) for any existing caller.
def detect_soul_unlocks(prompt: str) -> set[str]:
    return detect_read_unlocks(prompt)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sovereign_soul_grants (
    session_id TEXT NOT NULL,
    soul_id    TEXT NOT NULL,
    operation  TEXT NOT NULL,
    grant_id   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (session_id, soul_id, operation)
)
"""


def _db(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".index" / "soul_grants.sqlite3"


def _conn(project_root: Path) -> sqlite3.Connection:
    db = _db(project_root)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(_SCHEMA)
    return conn


def set_turn_grants(
    project_root: Path,
    session_id: str,
    read_souls: set[str],
    write_souls: set[str] | None = None,
) -> None:
    """REPLACE this session's soul grants with the current turn's. Read
    souls get an OP_READ grant, write souls an OP_WRITE grant. Per-turn:
    a prompt that names nothing clears all prior grants (door re-seals).
    Fail-closed: any storage error leaves nothing granted.
    """
    sid = (session_id or "").strip()
    if not sid:
        return
    write_souls = write_souls or set()
    expires = time.time() + _GRANT_TTL_SECONDS
    try:
        conn = _conn(project_root)
        try:
            conn.execute(
                "DELETE FROM sovereign_soul_grants WHERE session_id = ?",
                (sid,),
            )
            for soul in sorted(read_souls):
                conn.execute(
                    "INSERT OR REPLACE INTO sovereign_soul_grants "
                    "(session_id, soul_id, operation, grant_id, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, soul, OP_READ, secrets.token_hex(16), expires),
                )
            for soul in sorted(write_souls):
                conn.execute(
                    "INSERT OR REPLACE INTO sovereign_soul_grants "
                    "(session_id, soul_id, operation, grant_id, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sid, soul, OP_WRITE, secrets.token_hex(16), expires),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def consume_grant(
    project_root: Path,
    session_id: str,
    soul_id: str,
    operation: str,
) -> bool:
    """Atomically CONSUME (single-use) an exact (session, soul, operation)
    grant. Returns True only if a non-expired grant existed; the grant is
    deleted so it cannot authorize a second operation. Fail-closed: empty
    session/soul, unknown operation, expired, or any error → False.
    """
    sid = (session_id or "").strip()
    soul = (soul_id or "").strip()
    if not sid or not soul or operation not in (OP_READ, OP_WRITE):
        return False
    try:
        conn = _conn(project_root)
        try:
            row = conn.execute(
                "SELECT expires_at FROM sovereign_soul_grants "
                "WHERE session_id = ? AND soul_id = ? AND operation = ?",
                (sid, soul, operation),
            ).fetchone()
            if row is None:
                return False
            # Consume regardless (single-use), then honor expiry.
            conn.execute(
                "DELETE FROM sovereign_soul_grants "
                "WHERE session_id = ? AND soul_id = ? AND operation = ?",
                (sid, soul, operation),
            )
            conn.commit()
            return float(row[0]) >= time.time()
        finally:
            conn.close()
    except Exception:
        return False
