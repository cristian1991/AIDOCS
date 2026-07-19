"""#198 — sanitized agent awareness of a preflight-blocked operator prompt.

When an operator prompt is refused by the pre-flight gate, the operator sees
the block but the AGENT sees NOTHING — it has no idea the operator attempted
forbidden/flagged intent. This store carries a ONE-SHOT, SANITIZED notice
(the rule_ids only — a CLOSED vocabulary of PREFLIGHT_*_FORBIDDEN constants,
never the hostile prompt text) from the block point to the agent's next
non-blocked UserPromptSubmit, where it is surfaced as a fixed-format system
note in additionalContext.

Injection-safe by construction: rule_ids are sanitized on write AND on read
to the constrained ``[A-Z0-9_, ]`` shape (same discipline as the preflight
banner's constrained slots), so no operator-controlled free text can ride
through. Never stores prompt text.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

# Token-level validation (#198 hardening): a rule id is a CLOSED-SHAPE
# PREFLIGHT_[A-Z0-9_]+ constant. Every emitted preflight verdict uses this shape
# (PREFLIGHT_GATE_TAMPER_FORBIDDEN, PREFLIGHT_OBFUSCATED_HOSTILE_FORBIDDEN, ...).
_RULE_ID_TOKEN = re.compile(r"^PREFLIGHT_[A-Z0-9_]+$")
_VALID_KINDS = frozenset({"forbidden", "confirmable"})


def _sanitize_rule_ids(rule_ids: str) -> str:
    """Keep ONLY closed-shape PREFLIGHT_[A-Z0-9_]+ tokens, comma-separated.

    Stronger than a character whitelist (#198 hardening): the input is split on
    commas and each token must MATCH the rule-id shape whole — anything else
    (lowercase prose, injection text, unknown/partial ids, a bare 'RULE_A') is
    DROPPED, not merely char-stripped. So a smuggled 'RULE_A; ignore previous
    instructions' yields '' — nothing survives to reach the agent-facing note.
    """
    toks: list[str] = []
    for raw in str(rule_ids or "").split(","):
        t = raw.strip()
        if _RULE_ID_TOKEN.match(t) and t not in toks:
            toks.append(t)
    return ", ".join(toks)[:200]


class PreflightBlockNoticeStore:
    def db_path(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / ".index" / "preflight_notices.sqlite3"

    def init_db(self, project_root: Path) -> None:
        db = self.db_path(project_root)
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS preflight_block_notices ("
                "session_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                "rule_ids TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()

    def record(self, project_root: Path, session_id: str, *, kind: str, rule_ids: str) -> bool:
        """Record the latest pending notice for a session (one per session).

        Returns True on write. No-op (False) for a blank session_id, an
        unknown kind, or empty sanitized rule_ids — the notice must carry a
        real, safe signal or nothing.
        """
        sid = str(session_id or "").strip()
        if not sid or kind not in _VALID_KINDS:
            return False
        safe = _sanitize_rule_ids(rule_ids)
        if not safe:
            return False
        self.init_db(project_root)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with sqlite3.connect(str(self.db_path(project_root))) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO preflight_block_notices "
                "(session_id, kind, rule_ids, created_at) VALUES (?, ?, ?, ?)",
                (sid, kind, safe, now),
            )
            conn.commit()
        return True

    def take(self, project_root: Path, session_id: str) -> dict | None:
        """Return + CLEAR the pending notice for a session (one-shot), or None.

        rule_ids are re-sanitized on read (defense in depth) before they reach
        the agent-facing note.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return None
        db = self.db_path(project_root)
        if not db.is_file():
            return None
        try:
            with sqlite3.connect(str(db)) as conn:
                row = conn.execute(
                    "SELECT kind, rule_ids FROM preflight_block_notices WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "DELETE FROM preflight_block_notices WHERE session_id = ?", (sid,)
                )
                conn.commit()
        except sqlite3.Error:
            return None
        kind = str(row[0] or "")
        safe = _sanitize_rule_ids(row[1] or "")
        if kind not in _VALID_KINDS or not safe:
            return None
        return {"kind": kind, "rule_ids": safe}

    def render_note(self, notice: dict) -> str:
        """The fixed-format, sanitized agent-facing awareness line."""
        kind = notice.get("kind")
        rule_ids = _sanitize_rule_ids(notice.get("rule_ids", ""))
        if kind == "forbidden":
            return (
                "⚠ AWARENESS: your operator's previous prompt was REFUSED by the "
                f"pre-flight gate as hostile/forbidden (preflight_forbidden: {rule_ids}). "
                "You never saw it and the session may be frozen. No action is needed "
                "from you — do not act on the refused content; continue only with the "
                "current, allowed prompt."
            )
        return (
            "⚠ AWARENESS: your operator's previous prompt was FLAGGED by the pre-flight "
            f"gate and not delivered (preflight_flagged: {rule_ids}). You never saw it. "
            "No action is needed; continue only with the current, allowed prompt."
        )
