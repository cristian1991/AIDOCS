"""XAACP ranks and nicknames: two LABELS on an actor, neither a permission.

Operator design (approved): "we can keep ranks and nicknames separate. i give
ranks, and you guys choose nicknames".

RANK -- operator-assigned and authoritative, ``r<x><a|b><lane>-<gen>``:
``a`` conductor, ``b`` co-conductor, ``<lane>`` the lane number, ``<gen>`` the
generation (``r0b2-11`` is the 11th co-conductor of lane 2). One holder per
BASE rank (``r0b2``) per session at a time; a new holder takes the next
generation and the previous holder keeps its row as history. An explicit
generation is accepted only above the line's current maximum, so history is
never rewritten. An actor holds one rank at a time.

THE ONE WRITER is :func:`apply_operator_rank`, and it takes an explicit
:class:`OperatorAttestation` naming the operator channel and the operator's
user id. The channels are the Claude Code UserPromptSubmit phrase
(``ups``, hook_pipeline._ups_xaacp_rank_leg) and the dashboard admin command
(``dashboard``, cli.cmd_dashboard_xaacp_rank). A later channel (e.g. MCP
elicitation) is ONE new caller plus one entry in OPERATOR_RANK_CHANNELS; no
agent-reachable tool calls this, which tests/coordination/
test_xaacp_ranks_nicknames.py pins by AST.

NICKNAME -- agent-chosen and cosmetic, set by the actor for ITSELF only
(ai_msg mode='xaacp_nickname'). 2-24 chars, unique per session ignoring case,
never rank-shaped, never a seat role alias, never an existing actor id.

Both live in the project's conductor_comms store (additive tables).

WHERE A RANK IS WRITTEN == WHERE IT IS READ. Sends and the directory read
the XAACP AUTHORITY's store. On a local-authority project that is this store,
and both operator channels write it. On a gate-routed project it is the
gate's store, and the operator channels here REFUSE (``rank_authority_is_gate``)
rather than write a rank no send would see -- and they do not forward: the
edge carries every ai_msg call, an agent's included, on one machine gate
credential, so a forwarded attestation is a client claim the gate cannot
verify. A gate-routed rank needs a gate-NATIVE operator channel whose proof an
agent cannot produce (the gate web dashboard on its human session credential,
or an MCP elicitation the gate issues and the human answers), executing
:func:`apply_operator_rank` gate-side with the gate's own attestation.

Nicknames have no such problem: ai_msg forwards xaacp_nickname to the
authority with the caller's DERIVED identity, so the gate writes its store.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: Input form of a rank. Lowercase only; generation 1+ when given.
RANK_RE = re.compile(r"^r(\d+)([ab])(\d+)(?:-([1-9]\d*))?$")
#: Anything that LOOKS like a rank (any case, any generation) -- refused as a
#: nickname so a nickname can never be mistaken for one.
_RANK_LOOKALIKE_RE = re.compile(r"^r\d+[ab]\d+(?:-\d*)?$", re.IGNORECASE)
NICKNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,23}$")

#: The operator-attested channels that may assign a rank. 'elicitation' (MCP
#: server-triggered prompt answered by the human) is the anticipated third
#: channel; it is NOT accepted until its caller exists.
OPERATOR_RANK_CHANNELS = frozenset({"ups", "dashboard"})


@dataclass(frozen=True)
class OperatorAttestation:
    """Who attested a rank change, and through which operator channel.

    ``user_id`` is the authenticated operator (the audit's ``assigned_by``);
    ``evidence`` is free provenance for the audit (host session, command).
    Constructed ONLY by the operator-channel callers (pinned by test)."""

    channel: str
    user_id: str
    evidence: str = ""

    def problem(self) -> str:
        if str(self.channel or "").strip() not in OPERATOR_RANK_CHANNELS:
            return f"channel {self.channel!r} is not an operator rank channel"
        if not str(self.user_id or "").strip():
            return "no authenticated operator user id"
        return ""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Additive DDL; runs inside conductor_comms' schema transaction."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xaacp_ranks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            base_rank TEXT NOT NULL,
            generation INTEGER NOT NULL,
            assigned_at REAL NOT NULL,
            revoked_at REAL,
            assigned_by TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT '',
            revoked_by TEXT NOT NULL DEFAULT '',
            UNIQUE (session_id, base_rank, generation)
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_xaacp_ranks_one_holder "
        "ON xaacp_ranks (session_id, base_rank) WHERE revoked_at IS NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_xaacp_ranks_one_rank_per_actor "
        "ON xaacp_ranks (session_id, actor_id) WHERE revoked_at IS NULL"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS xaacp_nicknames (
            session_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            nickname TEXT NOT NULL,
            nickname_key TEXT NOT NULL,
            set_at REAL NOT NULL,
            PRIMARY KEY (session_id, actor_id),
            UNIQUE (session_id, nickname_key)
        )
        """
    )


# ── pure helpers ────────────────────────────────────────────────────────────


def parse_rank(text: str) -> tuple[str, int | None] | None:
    """``'r0b2-11'`` -> ``('r0b2', 11)``; ``'r0b2'`` -> ``('r0b2', None)``;
    anything else -> None. Strict: no whitespace, lowercase only."""
    m = RANK_RE.match(str(text or ""))
    if m is None or str(text) != m.group(0):
        return None
    base = f"r{int(m.group(1))}{m.group(2)}{int(m.group(3))}"
    if base != str(text).split("-")[0]:
        return None  # leading zeros etc. are not a second spelling of a rank
    gen = int(m.group(4)) if m.group(4) else None
    return base, gen


def format_rank(base: str, generation: int) -> str:
    return f"{base}-{int(generation)}"


def looks_like_rank(text: str) -> bool:
    return bool(_RANK_LOOKALIKE_RE.match(str(text or "").strip()))


#: A direct command line: at most 3 leading spaces (4+ or a tab is a markdown
#: indented code block), the lowercase keyword FIRST, no other text.
_UPS_RANK_LINE = re.compile(r"^ {0,3}rank[ \t]+([^\s`'\"‘’“”>]+)[ \t]+(r\d\S*?)[ \t]*$")
_UPS_UNRANK_LINE = re.compile(r"^ {0,3}unrank[ \t]+([^\s`'\"‘’“”>]+)[ \t]*$")
#: A fenced code block OPENER (CommonMark: ``` or ~~~, up to 3 spaces, an
#: optional info string).
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
#: A CLOSER: the fence run and nothing after it but spaces/tabs (no info
#: string). It closes only when it uses the opener's character and is at
#: least as long.
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
#: Characters that mean the line is quoted, code, or not a bare command.
_QUOTING = ("`", '"', "'", "‘", "’", "“", "”")


def _operator_command_lines(operator_text: str):
    """Lines of operator text that can be a DIRECT command.

    Skipped: every line inside a fenced code block (an unclosed fence runs to
    the end -- the safe direction), blockquotes (``>``), and any line carrying
    a backtick or a quote mark (inline code, quoted examples). Indented code
    blocks, list items and lines that open with framing ("e.g.", "if ...",
    "you could ...") never match because the command regex demands the
    keyword first with at most 3 leading spaces."""
    fence = ""
    for line in str(operator_text or "").splitlines():
        if fence:
            close = _FENCE_CLOSE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = ""
            continue
        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
            continue
        if line.lstrip().startswith(">"):
            continue
        if any(q in line for q in _QUOTING):
            continue
        yield line


def parse_operator_rank_commands(operator_text: str) -> list[dict]:
    """Exact-syntax STANDALONE command lines in provenance-floored operator text.

    ``rank <actor_id> <rank>`` and ``unrank <actor_id>``, each alone on its own
    line, lowercase keyword first. Markdown-aware (r0b2 block on c03acc38c):
    the provenance floor strips harness/peer/paste blocks but NOT ordinary
    markdown, so fenced code, inline code, blockquotes, quoted lines and
    example/conditional phrasing are never commands (see
    :func:`_operator_command_lines`). No NLP: prose ("rank these by priority")
    is not a command because its third token is not rank-shaped. A
    rank-shaped but malformed token IS a command, so the operator is told why
    it was not applied."""
    out: list[dict] = []
    for line in _operator_command_lines(operator_text):
        m = _UPS_RANK_LINE.match(line)
        if m:
            out.append({"op": "assign", "actor_id": m.group(1), "rank": m.group(2)})
            continue
        m = _UPS_UNRANK_LINE.match(line)
        if m:
            out.append({"op": "revoke", "actor_id": m.group(1), "rank": ""})
    return out


# ── reads (any caller) ──────────────────────────────────────────────────────


def current_rank(conn: sqlite3.Connection, session_id: str, actor_id: str) -> str:
    row = conn.execute(
        "SELECT base_rank, generation FROM xaacp_ranks "
        "WHERE session_id=? AND actor_id=? AND revoked_at IS NULL",
        (str(session_id), str(actor_id)),
    ).fetchone()
    return format_rank(row["base_rank"], row["generation"]) if row else ""


def identities(conn: sqlite3.Connection, session_id: str, actor_ids) -> dict[str, dict]:
    """``{actor_id: {"rank": ..., "nickname": ...}}`` ('' when none)."""
    ids = sorted({str(a or "").strip() for a in actor_ids if str(a or "").strip()})
    out = {a: {"rank": "", "nickname": ""} for a in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    for row in conn.execute(
        f"SELECT actor_id, base_rank, generation FROM xaacp_ranks "
        f"WHERE session_id=? AND revoked_at IS NULL AND actor_id IN ({marks})",
        (str(session_id), *ids),
    ):
        out[row["actor_id"]]["rank"] = format_rank(row["base_rank"], row["generation"])
    for row in conn.execute(
        f"SELECT actor_id, nickname FROM xaacp_nicknames WHERE session_id=? AND actor_id IN ({marks})",
        (str(session_id), *ids),
    ):
        out[row["actor_id"]]["nickname"] = str(row["nickname"] or "")
    return out


def resolve_alias(conn: sqlite3.Connection, session_id: str, target: str) -> dict | None:
    """A send target that is a RANK or a NICKNAME -> its actor.

    Returns None when ``target`` is neither shape-wise (the caller goes on to
    its other resolution). For a rank: ``{"actor_id", "via": "rank"}`` or
    ``{"actor_id": "", "via": "rank"}`` when no one holds it. A base rank
    names the CURRENT holder; an exact ``base-gen`` names that generation's
    actor, historical or current. A nickname matches ignoring case.
    """
    sid = str(session_id or "").strip()
    text = str(target or "").strip()
    parsed = parse_rank(text)
    if parsed is not None:
        base, gen = parsed
        if gen is None:
            row = conn.execute(
                "SELECT actor_id FROM xaacp_ranks WHERE session_id=? AND base_rank=? AND revoked_at IS NULL",
                (sid, base),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT actor_id FROM xaacp_ranks WHERE session_id=? AND base_rank=? AND generation=?",
                (sid, base, gen),
            ).fetchone()
        return {"actor_id": str(row["actor_id"]) if row else "", "via": "rank"}
    if NICKNAME_RE.match(text):
        row = conn.execute(
            "SELECT actor_id FROM xaacp_nicknames WHERE session_id=? AND nickname_key=?",
            (sid, text.lower()),
        ).fetchone()
        if row:
            return {"actor_id": str(row["actor_id"]), "via": "nickname"}
    return None


def _session_actor_ids(conn: sqlite3.Connection, session_id: str) -> set[str]:
    ids = {
        str(r["actor_id"])
        for r in conn.execute("SELECT actor_id FROM xaacp_actors WHERE session_id=?", (session_id,))
    }
    ids |= {
        str(r["actor_id"])
        for r in conn.execute(
            "SELECT actor_id FROM msg_role_map WHERE session_id=? AND actor_id != ''", (session_id,)
        )
    }
    return ids


# ── the ONE rank writer ─────────────────────────────────────────────────────


def _refusal(status: str, error: str, **extra) -> dict:
    return {"ok": False, "status": status, "error": error, **extra}


def apply_operator_rank(
    project_root: Path,
    *,
    session_id: str,
    actor_id: str,
    rank: str,
    attestation: OperatorAttestation,
    op: str = "assign",
) -> dict:
    """Assign (``op='assign'``) or revoke (``op='revoke'``) an actor's rank.

    Every call must carry an :class:`OperatorAttestation` from an operator
    channel; anything else writes nothing (``not_attested``). Audited with
    the operator's user id and channel."""
    from . import conductor_comms as cc

    if not isinstance(attestation, OperatorAttestation) or attestation.problem():
        why = attestation.problem() if isinstance(attestation, OperatorAttestation) else "no operator attestation"
        return _refusal("not_attested", f"rank change refused: {why}")
    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    if not sid or not aid:
        return _refusal("invalid", "rank change needs session_id and actor_id")
    if op not in {"assign", "revoke"}:
        return _refusal("invalid", f"unknown rank op {op!r}")
    parsed = None
    if op == "assign":
        parsed = parse_rank(str(rank or "").strip())
        if parsed is None:
            return _refusal(
                "bad_format",
                f"{rank!r} is not a rank (expected r<x><a|b><lane>[-<generation>], e.g. r0b2 or r0b2-11)",
                actor_id=aid,
            )
    refused = cc._xaacp_authority_refusal(project_root)
    if refused is not None:
        # WRITE STORE == READ STORE. On a gate-routed project the send
        # resolver and the directory read the GATE's store, so a rank written
        # here would be invisible to them. It is NOT forwarded either: the edge
        # forwards every ai_msg call (an agent's included) with this machine's
        # one gate credential, so the gate could not tell this operator write
        # from an agent's. A gate-routed rank needs a gate-native operator
        # channel (see module docstring); until then, refuse and write nothing.
        return _refusal(
            "rank_authority_is_gate",
            "this project's XAACP authority is the gate (sends and the directory read the gate's "
            "store), so nothing was written: a local rank would be invisible to them, and a "
            "forwarded one could not be verified there as operator-originated. Assign ranks "
            "through a gate-side operator channel (not built yet)",
            actor_id=aid,
            authority_status=str(refused.get("status") or ""),
        )
    user_id = str(attestation.user_id).strip()
    channel = str(attestation.channel).strip()
    now = time.time()
    with cc._connect(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if aid not in _session_actor_ids(conn, sid):
                conn.rollback()
                return _refusal(
                    "unknown_actor", f"no XAACP actor {aid!r} is known on session {sid!r}", actor_id=aid
                )
            held = conn.execute(
                "SELECT id, base_rank, generation FROM xaacp_ranks "
                "WHERE session_id=? AND actor_id=? AND revoked_at IS NULL",
                (sid, aid),
            ).fetchone()
            if op == "revoke":
                if held is None:
                    conn.rollback()
                    return {"ok": True, "status": "no_rank", "actor_id": aid, "session_id": sid,
                            "message": f"{aid} holds no rank on session {sid}"}
                conn.execute(
                    "UPDATE xaacp_ranks SET revoked_at=?, revoked_by=? WHERE id=?", (now, user_id, held["id"])
                )
                conn.commit()
                label = format_rank(held["base_rank"], held["generation"])
                out = {"ok": True, "status": "revoked", "actor_id": aid, "session_id": sid, "rank": label,
                       "message": f"Rank {label} revoked from {aid}"}
                action = "xaacp_rank_revoke"
            else:
                base, gen = parsed
                if held is not None and held["base_rank"] == base and gen is None:
                    conn.rollback()
                    label = format_rank(base, held["generation"])
                    return {"ok": True, "status": "unchanged", "actor_id": aid, "session_id": sid,
                            "rank": label, "message": f"Rank {label} already held by {aid}"}
                top = conn.execute(
                    "SELECT MAX(generation) AS g FROM xaacp_ranks WHERE session_id=? AND base_rank=?",
                    (sid, base),
                ).fetchone()
                current_max = int(top["g"] or 0)
                if gen is not None and gen <= current_max:
                    conn.rollback()
                    return _refusal(
                        "generation_not_increasing",
                        f"{format_rank(base, gen)} is not above the current generation "
                        f"{format_rank(base, current_max)}; history is never rewritten",
                        actor_id=aid,
                    )
                new_gen = gen if gen is not None else current_max + 1
                holder = conn.execute(
                    "SELECT id, actor_id FROM xaacp_ranks WHERE session_id=? AND base_rank=? AND revoked_at IS NULL",
                    (sid, base),
                ).fetchone()
                previous_holder = str(holder["actor_id"]) if holder else ""
                if holder is not None:
                    conn.execute(
                        "UPDATE xaacp_ranks SET revoked_at=?, revoked_by=? WHERE id=?", (now, user_id, holder["id"])
                    )
                if held is not None and (holder is None or held["id"] != holder["id"]):
                    # one rank per actor: its previous rank becomes history
                    conn.execute(
                        "UPDATE xaacp_ranks SET revoked_at=?, revoked_by=? WHERE id=?", (now, user_id, held["id"])
                    )
                conn.execute(
                    "INSERT INTO xaacp_ranks (session_id, actor_id, base_rank, generation, assigned_at, "
                    "assigned_by, channel) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, aid, base, new_gen, now, user_id, channel),
                )
                conn.commit()
                label = format_rank(base, new_gen)
                out = {"ok": True, "status": "assigned", "actor_id": aid, "session_id": sid, "rank": label,
                       "previous_holder": previous_holder,
                       "previous_rank": format_rank(held["base_rank"], held["generation"]) if held else "",
                       "message": f"Rank {label} assigned to {aid}"}
                action = "xaacp_rank_assign"
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
    cc._audit_event(
        project_root,
        sid,
        action_kind=action,
        target_entity=aid,
        payload={
            "session_id": sid,
            "actor_id": aid,
            "rank": out.get("rank", ""),
            "previous_holder": out.get("previous_holder", ""),
            "assigned_by": user_id,
            "channel": channel,
            "evidence": str(attestation.evidence or "")[:200],
        },
        event_kind="xaacp_rank",
    )
    return out


# ── the nickname writer (self only) ─────────────────────────────────────────


def set_own_nickname(project_root: Path, *, session_id: str, actor_id: str, nickname: str) -> dict:
    """Set or clear (empty) THE CALLER'S nickname. ``actor_id`` is the
    dispatcher's DERIVED caller actor -- never a request value."""
    from . import conductor_comms as cc

    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    nick = str(nickname or "").strip()
    if not sid or not aid:
        return _refusal("invalid", "nickname needs a bound session and actor")
    if nick:
        if not NICKNAME_RE.match(nick):
            return _refusal(
                "bad_nickname",
                "a nickname is 2-24 characters: a letter, then letters, digits, '_' or '-'",
            )
        if looks_like_rank(nick):
            return _refusal("nickname_reserved", f"{nick!r} looks like a rank; ranks are operator-assigned")
        if nick.lower() in {r.lower() for r in cc.MSG_ROLES}:
            return _refusal("nickname_reserved", f"{nick!r} is a seat role alias")
    now = time.time()
    with cc._connect(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if nick:
                if nick.lower() in {a.lower() for a in _session_actor_ids(conn, sid) if a != aid}:
                    conn.rollback()
                    return _refusal("nickname_reserved", f"{nick!r} is an actor id on this session")
                other = conn.execute(
                    "SELECT actor_id FROM xaacp_nicknames WHERE session_id=? AND nickname_key=? AND actor_id != ?",
                    (sid, nick.lower(), aid),
                ).fetchone()
                if other is not None:
                    conn.rollback()
                    return _refusal("nickname_taken", f"{nick!r} is already taken on this session")
                conn.execute(
                    "INSERT INTO xaacp_nicknames (session_id, actor_id, nickname, nickname_key, set_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, actor_id) DO UPDATE SET "
                    "nickname=excluded.nickname, nickname_key=excluded.nickname_key, set_at=excluded.set_at",
                    (sid, aid, nick, nick.lower(), now),
                )
            else:
                conn.execute("DELETE FROM xaacp_nicknames WHERE session_id=? AND actor_id=?", (sid, aid))
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
    cc._audit_event(
        project_root,
        sid,
        action_kind="xaacp_nickname_set" if nick else "xaacp_nickname_cleared",
        target_entity=aid,
        payload={"session_id": sid, "actor_id": aid, "nickname": nick},
        event_kind="xaacp_nickname",
    )
    return {
        "ok": True,
        "status": "set" if nick else "cleared",
        "session_id": sid,
        "actor_id": aid,
        "nickname": nick,
    }
