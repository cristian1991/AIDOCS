"""Conductor communication — SQLite-backed message queue between agents, conductors, and operators.

Three communication patterns (agent-callable tool in parentheses):
1. Agent → Conductor/Operator: agent asks a question, waits for answer (ai_qa action='ask')
2. Conductor/Operator → Agent: push guidance, agent sees it on next tool call (ai_guidance)
3. Lane control: pause/resume/expand scope (ai_lane)

SQLite is the transfer medium. Dashboard is the operator UI. Hooks inject messages into agent context.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

# #755: canonical connect — WAL, synchronous=NORMAL, busy_timeout and
# foreign_keys=ON, none of which raw sqlite3.connect() applies. This
# schema declares no FOREIGN KEYs, so enabling enforcement is inert here.
# row_factory stays ROW (the helper's default): this store read by name
# already, so the hand-set line it replaces is redundant.
from ._sqlite_connect import connect as _canonical_connect

logger = logging.getLogger("aidocs.conductor_comms")
# ONE LIVENESS CHECK, OWNED BY THE LEASE. Imported rather than re-implemented:
# `_window_process_is_alive` is the single place that checks BOTH HALVES of a
# window key -- pid AND creation filetime -- and it carries a documented
# Windows scar (2026-05-13: `os.kill(pid, 0)` there can leave a handle in a
# state that crashes the enclosing process's next stdio read, so it uses
# OpenProcess + GetExitCodeProcess). A private copy here would be a second
# chance to drop the creation-time half, which is the recycled-pid one-way
# door (#880 item 1), and a second chance to get the scar wrong.
from .window_binding_store import _window_process_is_alive as _seat_window_is_alive

logger = logging.getLogger("aidocs.conductor_comms")

_THINK_MODES = {"off", "low", "medium", "high"}
_TASK_DEFAULT_THINK_MODE = {
    "implement": "low",
    "refactor": "medium",
    "design": "high",
    "test": "low",
    "docs": "low",
    "research": "medium",
    "debug": "medium",
    "review": "high",
    "deploy": "medium",
}
_REASONING_VARIANT_SUFFIXES = (":fast", ":thinking", ":reasoning", ":deep")
_CAPABILITY_CACHE: dict[tuple[str, str, str], dict[str, object]] = {}
_FALLBACK_LOG_CACHE: set[tuple[str, str, str, str, str]] = set()


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "conductor_comms.sqlite3"


def _connect(project_root: Path) -> sqlite3.Connection:
    path = _db_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _canonical_connect(str(path), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            lane_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            response TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at REAL NOT NULL,
            answered_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lane_control (
            lane_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active',
            reason TEXT,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lane_scopes (
            lane_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL,
            PRIMARY KEY (lane_id, session_id, file_path)
        )
    """)
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    additive_columns = {
        "from_role": "TEXT NOT NULL DEFAULT ''",
        "to_roles_json": "TEXT NOT NULL DEFAULT '[]'",
        "thread_id": "TEXT NOT NULL DEFAULT ''",
        "protocol": "TEXT NOT NULL DEFAULT ''",
        "message_kind": "TEXT NOT NULL DEFAULT ''",
        "sender_actor_id": "TEXT NOT NULL DEFAULT ''",
        "target_actor_id": "TEXT NOT NULL DEFAULT ''",
        "correlation_id": "TEXT NOT NULL DEFAULT ''",
        "reply_to_id": "TEXT NOT NULL DEFAULT ''",
        "decision_status": "TEXT NOT NULL DEFAULT ''",
        # DRT-12 — RECEIPT IS NOT OUTCOME. `decision_status` holds a TERMINAL
        # fact, so before these three columns existed the protocol had no way to
        # say "I have this and I own the answer" without ALSO saying the work had
        # concluded. ACK lives in its own columns precisely so it can never be
        # mistaken for a decision: nothing reads them as a status, and a terminal
        # decision lands on the same row afterwards without touching them.
        #
        # `acked_at IS NULL` IS THE ONE TEST FOR "not yet acked" (there is no
        # sentinel 0.0 to disagree with), which is what makes the fire-once
        # UPDATE guard below a single statement instead of a read-then-write.
        "acked_at": "REAL",
        "acked_by": "TEXT NOT NULL DEFAULT ''",
        "ack_body": "TEXT NOT NULL DEFAULT ''",
        "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "wake_requested": "INTEGER NOT NULL DEFAULT 0",
        "expires_at": "REAL",
    }
    for column, ddl in additive_columns.items():
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {ddl}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS msg_role_map (
            host_session_id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            host_kind TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        )
    """)
    role_map_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(msg_role_map)").fetchall()
    }
    for column, ddl in {
        "actor_id": "TEXT NOT NULL DEFAULT ''",
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "host_kind": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "REAL NOT NULL DEFAULT 0",
        # WHERE THIS SEAT CAME FROM. Operator law is that every value names
        # its origin, so a later reader never has to say "we cannot tell from
        # where". Measured 2026-08-24: four rows with a blank scope and
        # updated_at=0.0, and the only way to attribute them to the legacy
        # migration below was to reason about that zero.
        "source": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in role_map_cols:
            conn.execute(f"ALTER TABLE msg_role_map ADD COLUMN {column} {ddl}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS xaacp_actors (
            actor_id TEXT PRIMARY KEY,
            host_session_id TEXT NOT NULL,
            host_kind TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL,
            actor_kind TEXT NOT NULL DEFAULT 'agent',
            role TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            last_seen_boot_token TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_actors_session "
        "ON xaacp_actors (session_id, actor_kind, updated_at)"
    )
    xaacp_actor_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(xaacp_actors)").fetchall()
    }
    if "role" not in xaacp_actor_cols:
        conn.execute("ALTER TABLE xaacp_actors ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if "last_seen_boot_token" not in xaacp_actor_cols:
        conn.execute(
            "ALTER TABLE xaacp_actors "
            "ADD COLUMN last_seen_boot_token TEXT NOT NULL DEFAULT ''"
        )
    # #1007: ONE registry, extended — never a parallel one. An actor row names
    # (aidocs_actor_id == actor_id, actor_kind conductor|subagent|lane_worker,
    # host_session_id, host_agent_id, worker_id, lane_id, session_id).
    # host_agent_id is the HOST-issued per-subagent id (CC `agent_id`), stored
    # under this name and never as bare `agent_id`, so a reader can never
    # mistake it for our own actor address.
    for column, ddl in {
        "host_agent_id": "TEXT NOT NULL DEFAULT ''",
        "worker_id": "TEXT NOT NULL DEFAULT ''",
        "lane_id": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in xaacp_actor_cols:
            conn.execute(f"ALTER TABLE xaacp_actors ADD COLUMN {column} {ddl}")
    # #1007 transport channel: a one-shot CALL CLAIM written by the in-subagent
    # PreToolUse hook (which holds CC's agent_id) and TAKEN by the daemon when
    # the matching MCP request arrives. See xaacp_record_call_claim.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS xaacp_call_claims (
            claim_id TEXT PRIMARY KEY,
            host_session_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_hash TEXT NOT NULL,
            host_agent_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_xaacp_call_claims_key "
        "ON xaacp_call_claims (host_session_id, tool_name, args_hash, created_at)"
    )
    # #1015 CLAIM CHANNEL WATERMARK. Claim rows expire (TTL); the PROOF that a
    # conversation's PreToolUse hook reaches this daemon must not, because that
    # proof is the only thing that tells "a host which never claims" (lane
    # workers, the Outer Gate, any non-Claude-Code surface -- unaffected) from
    # "a call this conversation's gate never saw" (fail closed). Sticky for the
    # life of the host session on purpose: an expiring watermark would re-open
    # the hole after an idle window.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS xaacp_claim_channel (
            host_session_id TEXT PRIMARY KEY,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_xaacp_actors_one_seat "
        "ON xaacp_actors (session_id, role) WHERE role != ''"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS msg_reads (
            message_id TEXT NOT NULL,
            role TEXT NOT NULL,
            read_at REAL NOT NULL,
            PRIMARY KEY (message_id, role)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_xaacp_route "
        "ON messages (direction, session_id, target_actor_id, lane_id, status, created_at)"
    )
    legacy_role_map = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='cerberus_role_map'"
    ).fetchone()
    if legacy_role_map is not None:
        # AN UNSCOPED SEAT, NAMED AS SUCH. `cerberus_role_map` predates the
        # session scope and carries only (host_session_id, role), so this
        # migration CANNOT produce a scoped row -- and it must not invent one.
        # What it can do is say where the row came from, so an unscoped
        # conductor row found years later is explainable rather than a
        # mystery. `seat_scope_matches` is what makes the blank scope safe:
        # such a row is a conductor of NO session, never of every session.
        conn.execute(
            "INSERT OR IGNORE INTO msg_role_map "
            "(host_session_id, role, source) "
            "SELECT host_session_id, role, ? FROM cerberus_role_map",
            (MSG_SEAT_SOURCE_LEGACY,),
        )
        conn.execute("DROP TABLE cerberus_role_map")
    legacy_reads = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='cerberus_reads'"
    ).fetchone()
    if legacy_reads is not None:
        conn.execute(
            "INSERT OR IGNORE INTO msg_reads (message_id, role, read_at) "
            "SELECT message_id, role, read_at FROM cerberus_reads"
        )
        conn.execute("DROP TABLE cerberus_reads")
    conn.execute("UPDATE messages SET direction = 'msg' WHERE direction = 'cerberus'")
    # #1061: THE ONE ORDERED ACTOR-MESSAGE STREAM lives beside `messages`, in
    # the same store and transaction; its first open migrates every pre-stream
    # XAACP row into it (empire XVIII -- nothing orphaned).
    from .xaacp_stream import ensure_schema as _ensure_stream_schema

    _ensure_stream_schema(conn)
    conn.commit()
    return conn



def register_lane_scope(
    project_root: Path,
    lane_id: str,
    files: list[str],
    *,
    session_id: str = "",
) -> None:
    """Register a lane's file scope. Called when conductor dispatches a lane."""
    normalized = [f.replace("\\", "/").strip() for f in files if f.strip()]
    with _connect(project_root) as conn:
        # Clear old scope for this lane
        conn.execute(
            "DELETE FROM lane_scopes WHERE lane_id = ? AND session_id = ?",
            (lane_id, session_id),
        )
        for f in normalized:
            conn.execute(
                "INSERT OR IGNORE INTO lane_scopes (lane_id, session_id, file_path) VALUES (?, ?, ?)",
                (lane_id, session_id, f),
            )
        conn.commit()


def get_lane_scope(project_root: Path, lane_id: str, session_id: str = "") -> list[str]:
    """Get a lane's registered file scope."""
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT file_path FROM lane_scopes WHERE lane_id = ? AND session_id = ?",
            (lane_id, session_id),
        ).fetchall()
    return [r["file_path"] for r in rows]


def check_scope_conflict(
    project_root: Path,
    lane_id: str,
    file_path: str,
    session_id: str = "",
) -> str | None:
    """Check if any OTHER lane has this file in scope. Returns conflicting lane_id or None."""
    normalized = file_path.replace("\\", "/").strip()
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT lane_id FROM lane_scopes WHERE file_path = ? AND session_id = ? AND lane_id != ?",
            (normalized, session_id, lane_id),
        ).fetchone()
    return row["lane_id"] if row else None


# ── Agent → Conductor/Operator ──


def agent_ask(
    project_root: Path,
    lane_id: str,
    question: str,
    *,
    category: str = "question",
    session_id: str = "",
    wait: bool = False,
    timeout: float = 120.0,
    poll_interval: float = 2.0,
) -> dict:
    """Agent submits a question. If wait=True, polls until answered or timeout."""
    msg_id = str(uuid4())[:12]
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT INTO messages (id, lane_id, session_id, direction, category, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                lane_id,
                session_id,
                "agent_to_conductor",
                category,
                question,
                "pending",
                time.time(),
            ),
        )
        conn.commit()

    if not wait:
        return {
            "id": msg_id,
            "status": "pending",
            "message": "Question submitted to conductor. Continue with other work — the answer will appear as a conductor message on your next tool call once answered.",
        }

    # Poll for answer
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _connect(project_root) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if row and row["status"] == "answered":
                return {"id": msg_id, "status": "answered", "response": row["response"]}
        time.sleep(poll_interval)

    return {
        "id": msg_id,
        "status": "timeout",
        "message": f"No response within {timeout}s. Continue with your best judgment.",
    }


def check_response(project_root: Path, message_id: str) -> dict:
    """Check if a previously submitted question has been answered."""
    with _connect(project_root) as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if not row:
            return {"id": message_id, "status": "not_found"}
        if row["status"] == "answered":
            return {"id": message_id, "status": "answered", "response": row["response"]}
        return {"id": message_id, "status": row["status"]}


# ── Conductor/Operator → Agent ──


def send_guidance(
    project_root: Path,
    lane_id: str,
    message: str,
    *,
    session_id: str = "",
) -> dict:
    """Conductor/operator sends guidance to a lane agent. Agent sees it on next tool call."""
    msg_id = str(uuid4())[:12]
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT INTO messages (id, lane_id, session_id, direction, category, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                lane_id,
                session_id,
                "conductor_to_agent",
                "guidance",
                message,
                "pending",
                time.time(),
            ),
        )
        conn.commit()
    return {"id": msg_id, "sent": True}


def answer_question(project_root: Path, message_id: str, response: str) -> dict:
    """Conductor/operator answers an agent's question.

    Also creates a conductor→agent guidance message so the answer
    is automatically injected into the agent's next tool call via hook.
    """
    with _connect(project_root) as conn:
        # Get the original question's lane_id and session_id
        original = conn.execute(
            "SELECT lane_id, session_id FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not original:
            return {"answered": False, "reason": "Message not found"}

        cursor = conn.execute(
            "UPDATE messages SET response = ?, status = 'answered', answered_at = ? WHERE id = ? AND status = 'pending'",
            (response, time.time(), message_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"answered": False, "reason": "Already answered"}

        # Auto-create a guidance message so agent sees the answer on next tool call
        reply_id = str(uuid4())[:12]
        conn.execute(
            "INSERT INTO messages (id, lane_id, session_id, direction, category, content, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reply_id,
                original["lane_id"],
                original["session_id"],
                "conductor_to_agent",
                "answer",
                f"Re: your question — {response}",
                "pending",
                time.time(),
            ),
        )
        conn.commit()

        return {"answered": True, "id": message_id, "reply_id": reply_id}


def get_pending_for_agent(project_root: Path, lane_id: str) -> list[dict]:
    """Get unread conductor→agent messages for a lane. Marks them as read."""
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lane_id = ? AND direction = 'conductor_to_agent' AND status = 'pending' ORDER BY created_at",
            (lane_id,),
        ).fetchall()
        if rows:
            ids = [row["id"] for row in rows]
            conn.execute(
                f"UPDATE messages SET status = 'read' WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            conn.commit()
        return [
            {"id": row["id"], "category": row["category"], "content": row["content"]}
            for row in rows
        ]


def get_pending_questions(project_root: Path, session_id: str = "") -> list[dict]:
    """Get all pending agent→conductor questions (for dashboard display)."""
    with _connect(project_root) as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' AND session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' ORDER BY created_at",
            ).fetchall()
        return [
            {
                "id": row["id"],
                "lane_id": row["lane_id"],
                "category": row["category"],
                "content": row["content"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]


# ── Lane Control ──


def set_lane_state(
    project_root: Path,
    lane_id: str,
    state: str,
    *,
    reason: str = "",
    session_id: str = "",
) -> dict:
    """Set lane state: active, paused, canceled."""
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO lane_control (lane_id, session_id, state, reason, updated_at) VALUES (?, ?, ?, ?, ?)",
            (lane_id, session_id, state, reason, time.time()),
        )
        conn.commit()
    return {"lane_id": lane_id, "state": state, "reason": reason}


def get_lane_state(project_root: Path, lane_id: str) -> dict:
    """Check if a lane is paused/canceled."""
    with _connect(project_root) as conn:
        row = conn.execute("SELECT * FROM lane_control WHERE lane_id = ?", (lane_id,)).fetchone()
        if not row:
            return {"lane_id": lane_id, "state": "active"}
        return {
            "lane_id": row["lane_id"],
            "state": row["state"],
            "reason": row["reason"] or "",
        }


def check_lane_and_messages(project_root: Path, lane_id: str) -> dict:
    """Combined check for hook — lane state + pending messages in one call."""
    lane = get_lane_state(project_root, lane_id)
    messages = get_pending_for_agent(project_root, lane_id)
    return {
        "lane_state": lane["state"],
        "lane_reason": lane.get("reason", ""),
        "pending_messages": messages,
    }


# ── Conductor Situational Awareness ──


def get_all_lanes_status(project_root: Path, session_id: str = "") -> dict:
    """Full picture for conductor: all lanes, states, pending questions, recent activity.

    #54 — THE FALSE ZERO. This overview used to read ONLY ``lane_control``,
    whose sole writer is ``set_lane_state`` (the conductor's manual
    pause/resume/cancel override). The spawn path writes
    ``session_lane_agents`` and never touches ``lane_control``, so
    ``ai_seat(mode='overview')`` reported zero live lanes while six were
    running — a directory that was not being written to. The lane roster is
    now sourced from ``session_lane_agents`` (the table the spawn path
    writes), with ``lane_control`` merged back in as what it actually is: a
    manual OVERRIDE. An override rules the displayed state only while the
    registry says the worker is live; a terminal registry state
    (done/failed/crashed/killed) is the stronger truth. Control-only rows
    (no registry twin) still surface — the merge widens, never narrows.
    """
    sid = str(session_id or "").strip()

    registry_rows: list[dict] = []
    try:
        from .session_lane_agents_store import SessionLaneAgentsStore

        registry_rows = [
            row
            for row in SessionLaneAgentsStore().get_all_lane_agents(project_root)
            if not sid or str(row.get("session_id") or "").strip() == sid
        ]
    except Exception:
        logger.exception(
            "lane registry read failed; overview degrades to lane_control only",
        )

    with _connect(project_root) as conn:
        # Manual overrides (pause/cancel) — merged over the registry below.
        if sid:
            control = conn.execute(
                "SELECT * FROM lane_control WHERE session_id = ? ORDER BY lane_id",
                (sid,),
            ).fetchall()
        else:
            control = conn.execute("SELECT * FROM lane_control ORDER BY lane_id").fetchall()

        # Pending questions
        if sid:
            questions = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' AND session_id = ? ORDER BY created_at",
                (sid,),
            ).fetchall()
        else:
            questions = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' ORDER BY created_at",
            ).fetchall()

        # Recent messages (last 20)
        recent = conn.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT 20").fetchall()

    control_by_lane = {str(r["lane_id"] or ""): r for r in control}
    lanes: list[dict] = []
    seen: set[str] = set()
    for row in registry_rows:
        lane_id = str(row.get("lane_id") or "")
        seen.add(lane_id)
        registry_state = str(row.get("state") or "")
        override = control_by_lane.get(lane_id)
        override_state = str(override["state"] or "") if override is not None else ""
        if (
            override is not None
            and registry_state == "running"
            and override_state not in ("", "active")
        ):
            state = override_state
            reason = str(override["reason"] or "")
            updated_at = override["updated_at"]
        else:
            state = registry_state
            reason = ""
            updated_at = row.get("updated_at")
        lanes.append(
            {
                "lane_id": lane_id,
                "state": state,
                "reason": reason,
                "updated_at": updated_at,
                "worker_id": str(row.get("worker_id") or ""),
                "backend": str(row.get("backend") or ""),
                "session_id": str(row.get("session_id") or ""),
                "registry_state": registry_state,
                "control_state": override_state,
                "source": "registry",
            },
        )
    for r in control:
        lane_id = str(r["lane_id"] or "")
        if lane_id in seen:
            continue
        lanes.append(
            {
                "lane_id": lane_id,
                "state": str(r["state"] or ""),
                "reason": str(r["reason"] or ""),
                "updated_at": r["updated_at"],
                "worker_id": "",
                "backend": "",
                "session_id": str(r["session_id"] or ""),
                "registry_state": "",
                "control_state": str(r["state"] or ""),
                "source": "control",
            },
        )

    return {
        "lanes": lanes,
        "pending_questions": [
            {
                "id": r["id"],
                "lane_id": r["lane_id"],
                "category": r["category"],
                "content": r["content"],
                "created_at": r["created_at"],
            }
            for r in questions
        ],
        "recent_messages": [
            {
                "id": r["id"],
                "lane_id": r["lane_id"],
                "direction": r["direction"],
                "category": r["category"],
                "content": r["content"][:100],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in recent
        ],
    }


# ── Auto-Resolution Policies ──


def auto_resolve_scope_request(
    project_root: Path,
    message_id: str,
    lane_id: str,
    requested_path: str,
    session_id: str = "",
) -> dict:
    """Auto-resolve a scope request if no conflict exists.

    Checks if any other lane has the requested file in its scope.
    If no conflict → auto-approve and expand scope.
    If conflict → leave pending for conductor/operator.
    """
    try:
        conflicting_lane = check_scope_conflict(project_root, lane_id, requested_path, session_id)

        if conflicting_lane:
            return {
                "auto_resolved": False,
                "reason": f"Conflict: lane '{conflicting_lane}' also has '{requested_path}' in scope",
                "message_id": message_id,
            }

        # No conflict — auto-approve
        # Add to lane's registered scope
        normalized = requested_path.replace("\\", "/").strip()
        with _connect(project_root) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO lane_scopes (lane_id, session_id, file_path) VALUES (?, ?, ?)",
                (lane_id, session_id, normalized),
            )
            conn.commit()

        # Also add to gate's known_exact_paths so the read gate allows it
        try:
            from .query_gate import QueryGateStore

            store = QueryGateStore()
            state = store.get(project_root, session_id)
            known = list(state.get("known_exact_paths") or [])
            if normalized not in known:
                known.append(normalized)
                store.set(project_root, session_id, known_exact_paths=known)
        except Exception:
            pass

        # Answer the question automatically
        answer_question(
            project_root,
            message_id,
            f"Auto-approved: '{requested_path}' added to your scope. No conflicts with other lanes.",
        )

        return {
            "auto_resolved": True,
            "path": requested_path,
            "message_id": message_id,
        }

    except Exception as exc:
        return {
            "auto_resolved": False,
            "reason": f"Auto-resolution failed: {exc}",
            "message_id": message_id,
        }


def get_message_history(project_root: Path, lane_id: str = "", limit: int = 50) -> list[dict]:
    """Get message history for a lane or all lanes."""
    with _connect(project_root) as conn:
        if lane_id:
            rows = conn.execute(
                "SELECT * FROM messages WHERE lane_id = ? ORDER BY created_at DESC LIMIT ?",
                (lane_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [
        {
            "id": r["id"],
            "lane_id": r["lane_id"],
            "direction": r["direction"],
            "category": r["category"],
            "content": r["content"],
            "response": r["response"],
            "status": r["status"],
            "created_at": r["created_at"],
            "answered_at": r["answered_at"],
        }
        for r in rows
    ]


# ── Task Routing ──


def _normalize_logical_think_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode not in _THINK_MODES:
        raise ValueError(f"Invalid think_mode '{value}'. Expected one of: {sorted(_THINK_MODES)}")
    return mode


def _strip_reasoning_variant(model: str) -> str:
    cleaned = str(model or "").strip()
    lower = cleaned.lower()
    for suffix in _REASONING_VARIANT_SUFFIXES:
        if lower.endswith(suffix):
            return cleaned[: -len(suffix)]
    return cleaned


def _provider_from_backend_model(backend: str, model: str) -> str:
    if backend == "claude":
        return "anthropic"
    if backend == "codex":
        return "openai"
    if backend == "opencode":
        parts = str(model or "").split("/", 1)
        if len(parts) == 2 and parts[0].strip():
            return parts[0].strip().lower()
        return "opencode"
    return backend


def resolve_backend_for_task(
    project_root: Path,
    task_type: str,
    session_id: str | None = None,
) -> dict:
    """Resolve host + model + think_mode for a task type from conductor.task_routing."""
    try:
        from .config import get_setting

        routing_raw = get_setting(
            "conductor.task_routing",
            project_root=project_root,
            session_id=session_id,
            default="{}",
        )
        routing = json.loads(routing_raw) if isinstance(routing_raw, str) else (routing_raw or {})
        if not isinstance(routing, dict):
            raise ValueError(
                "conductor.task_routing must be a JSON object mapping tasks to routing objects",
            )

        route = routing.get(task_type)
        if route is not None and not isinstance(route, dict):
            raise ValueError(
                f"conductor.task_routing.{task_type} must be an object with host/model/think_mode",
            )

        default_backend = str(
            get_setting(
                "conductor.backend",
                project_root=project_root,
                session_id=session_id,
                default="claude",
            ),
        )
        backend = str((route or {}).get("host") or default_backend).strip()
        if backend not in ("claude", "codex", "opencode"):
            raise ValueError(f"Invalid conductor host '{backend}' for task '{task_type}'")
        model_setting = {
            "claude": "conductor.claude_model",
            "codex": "conductor.codex_model",
            "opencode": "conductor.opencode_model",
        }.get(backend, "")
        default_model = (
            str(
                get_setting(
                    model_setting,
                    project_root=project_root,
                    session_id=session_id,
                    default="",
                )
                or "",
            )
            if model_setting
            else ""
        )
        explicit_think_mode = (
            _normalize_logical_think_mode((route or {}).get("think_mode"))
            if route and str((route or {}).get("think_mode") or "").strip()
            else None
        )
        model = str((route or {}).get("model") or default_model or "").strip()
        if explicit_think_mode and model:
            model = _strip_reasoning_variant(model)
        think_mode = explicit_think_mode or _normalize_logical_think_mode(
            get_setting(
                "conductor.think_mode",
                project_root=project_root,
                session_id=session_id,
                default=_TASK_DEFAULT_THINK_MODE.get(task_type, "medium"),
            ),
        )
        provider = _provider_from_backend_model(backend, model)
        native_param = "reasoning_effort"
        native_mode = think_mode
        fallback_used = False
        cache_key = (backend, provider, model)
        capability = _CAPABILITY_CACHE.get(cache_key)
        if capability is None:
            native_modes = ["off", "low", "medium", "high"]
            if backend == "opencode" and provider == "openrouter":
                native_modes = ["minimal", "low", "medium", "high"]
                native_param = "reasoning.effort"
            elif backend == "opencode" and provider in {"google", "lmstudio"}:
                native_modes = ["off", "low", "high"]
                native_param = "thinking"
            elif backend == "codex":
                native_modes = ["low", "medium", "high"]
            capability = {"native_modes": native_modes, "native_param": native_param}
            _CAPABILITY_CACHE[cache_key] = capability
        native_modes = (
            capability.get("native_modes")
            if isinstance(capability, dict)
            else ["off", "low", "medium", "high"]
        )
        native_param = (
            str(capability.get("native_param") or native_param)
            if isinstance(capability, dict)
            else native_param
        )
        if think_mode not in native_modes:
            fallback_used = True
            if backend == "opencode" and provider in {"google", "lmstudio"}:
                native_mode = {"medium": "high", "high": "high", "low": "low", "off": "off"}[
                    think_mode
                ]
            elif backend == "codex" and think_mode == "off":
                native_mode = "low"
            else:
                raise ValueError(
                    f"No deterministic think_mode translation for host={backend} provider={provider} model={model} logical_mode={think_mode}",
                )
            log_key = (session_id or "", backend, provider, model, think_mode)
            if log_key not in _FALLBACK_LOG_CACHE:
                logger.warning(
                    "think_mode fallback session=%s host=%s provider=%s model=%s logical=%s native=%s",
                    session_id or "",
                    backend,
                    provider,
                    model,
                    think_mode,
                    native_mode,
                )
                _FALLBACK_LOG_CACHE.add(log_key)
        return {
            "backend": backend,
            "model": model,
            "think_mode": think_mode,
            "think_mode_source": "task_route" if explicit_think_mode else "conductor_default",
            "native_think_mode": native_mode,
            "native_params": {native_param: native_mode},
            "fallback_used": fallback_used,
        }

    except Exception:
        return {
            "backend": "claude",
            "model": "",
            "think_mode": "medium",
            "think_mode_source": "conductor_default",
            "native_think_mode": "medium",
            "native_params": {"reasoning_effort": "medium"},
            "fallback_used": False,
        }


# ── Message Substrate (slice A) ──
# Role-addressed messaging available to all agents. Targets are
# predefined via MSG_ROLES (conductor / co_conductor / king today).
# Reuses `messages` table; adds from_role / to_roles_json / thread_id.
# Phoenix 2026-05-12: renamed from cerberus_* (Empire directive — one
# canonical name end-to-end, no internal-vs-external split).

MSG_ROLES = ("conductor", "co_conductor", "king")
# Fail-closed role for a caller whose host_session_id is NOT registered in
# msg_role_map (#215). It is deliberately NOT in MSG_ROLES, so msg_send rejects
# it (cannot post AS a seat) and msg_inbox for it matches no seat-addressed
# message (cannot drain a seat inbox). Identity is never a default.
MSG_ROLE_UNMAPPED = "unmapped"

#: `source` for a seat taken through `ai_seat enter` / `co-enter`.
MSG_SEAT_SOURCE_ENTER = "seat_enter"
#: `source` for a row carried over from the pre-scope `cerberus_role_map`.
MSG_SEAT_SOURCE_LEGACY = "legacy_cerberus_role_map"

def seat_scope_matches(row_session_id: object, session_id: object) -> bool:
    """Does a seat row scoped to *row_session_id* seat *session_id*?

    A SEAT IS A FACT ABOUT A SESSION. Operator law is "only 1 conductor and
    co-conductor can be ACTIVE ON AN AIDOCS SESSION", so `msg_role_map`'s
    `session_id` column is not decoration -- it is half the identity of the
    seat. Two readers threw it away (`agent_audit._roles_by_host` selected
    only `(host_session_id, role)`), which made every row answer for whatever
    session the agent happened to be bound to.

    A BLANK SCOPE IS A CONDUCTOR OF NO SESSION. Measured 2026-08-24: four of
    the nine live rows carry a blank scope, all from the legacy
    `cerberus_role_map` migration which copies `(host_session_id, role)` and
    nothing else. They are real rows and they are not deleted -- what they
    cannot do is answer a question they were never scoped to answer.

    BLANK NEVER MATCHES BLANK. The tempting one-liner is ``row == want``,
    which seats an unscoped row on an agent whose own session is also
    unknown -- two absences of information compared and called a match. The
    absence of a session is not a session, and it is not equal to itself, for
    the same reason `conversation_is_bound` refuses to collapse ``None`` into
    ``False``: a missing fact must never look like an affirmative one.
    """
    row = str(row_session_id or "").strip()
    want = str(session_id or "").strip()
    if not row or not want:
        return False
    return row == want


# ── WHAT A SEAT MAY BE KEYED ON (#880) ────────────────────────────────────
#
# #880 lists among the identity chain's measured defects: "append-only with
# NO FORMAT VALIDATION -- which is how `auth-truth-614`, a synthetic test id,
# is seated permanently in an authority structure." `msg_role_map` had no
# validation of any kind beyond a non-blank check.
#
# AT THE WRITER, NOT ONLY AT THE READER, for the reason
# `record_window_conversation` spells out for the lease: refusing a malformed
# key when you RESOLVE one protects the reader and not the TABLE. A seated
# junk row still occupies a PRIMARY KEY, is still counted and migrated and
# reported by a dashboard, and still cannot have its provenance explained by
# whoever finds it later -- "we cannot tell from where" under another name.
#
# TWO LAYERS, AND THE SPLIT IS THE DESIGN. AIDOCS MINTS a window key, so
# `WINDOW_KEY_SHAPE` can be exact. It does NOT mint a host_session_id -- the
# host does. One global shape for a value another program issues is the guess
# `window_key` refuses to make, and here the guess costs a LOCKOUT: no seat,
# no conducting, and no call the refused agent may still make can heal it.

#: LAYER 1 -- the floor, true for every host because it rests on nothing a
#: host is free to choose: an identity is ONE PRINTABLE TOKEN. Refuses the
#: blank, the whitespace-bearing, the control-character-bearing and the
#: absurd. `"  ..  "` is one of the four values #880 measured the lease
#: writer accepting before it was given a shape.
#:
#: AT LEAST ONE ALPHANUMERIC, and that clause is not cosmetic: the writer
#: `.strip()`s its input, so `"  ..  "` arrives here as `".."`, which a plain
#: printable-token rule ACCEPTS. An identity carrying no letter and no digit
#: encodes nothing at all, and `".."` in particular is a path token that has
#: no business keying an authority row.
HOST_SESSION_ID_FLOOR = re.compile(r"(?![!-~]{201})[!-~]*[0-9A-Za-z][!-~]*")

#: LAYER 2 -- the MEASURED shape, per host kind, for hosts AIDOCS has looked
#: at. `claude_code` mints a uuid4; measured 2026-08-24 on the operator's box,
#: every real row in `msg_role_map` is one of its two spellings (5 undashed,
#: 4 dashed). A host kind absent from this table gets the floor and NOTHING
#: MORE -- an unmeasured host is an honest limitation with a name, never a
#: licence to invent a pattern and lock that host out of its own seat.
HOST_SESSION_ID_SHAPES: dict[str, re.Pattern[str]] = {
    "claude_code": re.compile(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}",
    ),
}

#: The WORK-SESSION scope. This half AIDOCS really does own -- the shape is
#: already declared where the connector mints one
#: (`outer_gate_transport._ogt_pt_session_create`), so reusing it is not a
#: guess. Blank is legal and separately meaningful: see `seat_scope_matches`.
SEAT_SESSION_SCOPE_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def host_session_id_is_well_formed(
    host_session_id: object,
    host_kind: object = "",
) -> bool:
    """Could *host_session_id* be a real identity from *host_kind*?

    ONE DEFINITION, used by the writer. A private copy inside
    `msg_register_role` and a public constant for tests would drift an edit at
    a time, and the drift only ever shows up as rows the table holds that the
    declared shape says are impossible -- which is precisely the state #880
    found the identity chain in.
    """
    value = str(host_session_id or "")
    if not HOST_SESSION_ID_FLOOR.fullmatch(value):
        return False
    shape = HOST_SESSION_ID_SHAPES.get(str(host_kind or "").strip().lower())
    if shape is None:
        return True
    return bool(shape.fullmatch(value))


def _normalize_to_roles(to_roles: object) -> list[str]:
    """Accept str ('conductor'|'co_conductor'|'king'|'both' or comma-list)
    or list of strings. Returns a deduped, validated role list.
    """
    if isinstance(to_roles, str):
        token = to_roles.strip().lower()
        if token == "both":
            return ["conductor", "co_conductor"]
        if "," in token:
            parts = [p.strip().lower() for p in token.split(",") if p.strip()]
        else:
            parts = [token] if token else []
    elif isinstance(to_roles, (list, tuple)):
        parts = [str(p).strip().lower() for p in to_roles if str(p).strip()]
    else:
        raise ValueError("to_roles must be a string or a list of strings")
    out: list[str] = []
    for r in parts:
        if r not in MSG_ROLES:
            raise ValueError(f"Invalid role '{r}'. Expected one of: {list(MSG_ROLES)}")
        if r not in out:
            out.append(r)
    if not out:
        raise ValueError("to_roles must resolve to at least one role")
    return out


def msg_register_role(
    project_root: Path,
    host_session_id: str,
    role: str,
    *,
    session_id: str = "",
    actor_id: str = "",
    host_kind: str = "",
) -> dict:
    """Register one host-bound seat actor for legacy messages and XAACP.

    ``role`` remains the human seat label used by legacy send/inbox/reply.
    XAACP identity is the unique ``actor_id`` plus the exact work-session
    binding; two hosts may occupy the same role without collapsing into one
    actor. Existing rows self-heal when the current caller re-enters a seat.
    """
    role = str(role or "").strip().lower()
    host_session_id = str(host_session_id or "").strip()
    session_id = str(session_id or "").strip()
    actor_id = str(actor_id or "").strip()
    host_kind = str(host_kind or "").strip()
    if role not in MSG_ROLES:
        raise ValueError(f"Invalid role '{role}'. Expected one of: {list(MSG_ROLES)}")
    if not host_session_id:
        raise ValueError("host_session_id is required")

    with _connect(project_root) as conn:
        existing = conn.execute(
            "SELECT actor_id, session_id, host_kind FROM msg_role_map "
            "WHERE host_session_id=?",
            (host_session_id,),
        ).fetchone()
    if existing is not None:
        actor_id = actor_id or str(existing["actor_id"] or "").strip()
        session_id = session_id or str(existing["session_id"] or "").strip()
        host_kind = host_kind or str(existing["host_kind"] or "").strip()
    if not host_kind:
        try:
            from .mcp_server_runtime_helpers import current_calling_host_kind

            host_kind = str(current_calling_host_kind() or "").strip()
        except Exception:
            host_kind = ""
    # ── THE VALIDATION, AT THE WRITER, IN ONE PLACE ───────────────────────
    #
    # AND IT HAS TO BE HERE, not earlier. The per-host shape can only be
    # applied once `host_kind` is known, and `host_kind` is resolved just
    # above (argument, then the existing row, then the calling context).
    # Checking before that resolves would silently mean "floor only" for
    # every seat that lets the writer derive its host kind -- which is every
    # seat `ai_seat enter` takes on the real path.
    #
    # ONE SITE, DELIBERATELY. An early floor check plus this one reads like
    # defence in depth, but it means no single change can express "the writer
    # stopped validating" -- and that is the exact mutant this has to die to.
    # A gate that cannot state the failure it is guarding against is not
    # guarding against it.
    #
    # NON-DESTRUCTIVE. Everything executed before this point is a READ, so a
    # refusal here writes nothing and displaces nothing -- the same rule
    # `record_window_conversation` states for its own refusals ("The refusal
    # LEAVES ANY EXISTING ROW UNTOUCHED"). A validation that unseated the
    # sitting conductor on its way to rejecting somebody else's bad id would
    # be a worse bug than the one it fixes.
    if not host_session_id_is_well_formed(host_session_id, host_kind):
        raise ValueError(
            f"host_session_id {host_session_id!r} is not a well-formed "
            f"identity for host_kind {host_kind!r}. A seat may only be keyed "
            f"on one printable alphanumeric-bearing token, and on a host "
            f"AIDOCS has MEASURED it must additionally match what that host "
            f"mints. Seating anything else puts a row in an authority "
            f"structure that nobody can later explain (#880)",
        )
    # The scope. Blank stays legal -- it means "seats no session"
    # (`seat_scope_matches`), which is what the legacy migration's rows
    # honestly are. A NON-blank scope must be a work-session id AIDOCS could
    # have minted; that shape is already declared where the connector mints
    # one (`outer_gate_transport._ogt_pt_session_create`), so reusing it is
    # not a guess about somebody else's format.
    if session_id and not SEAT_SESSION_SCOPE_SHAPE.fullmatch(session_id):
        raise ValueError(
            f"session_id {session_id!r} is not a well-formed work-session "
            f"scope (expected [A-Za-z0-9._-]{{1,128}})",
        )
    if not actor_id:
        try:
            from .mcp_server_runtime_helpers import (
                current_calling_agent_context_id,
                current_calling_host_session_id,
            )

            if str(current_calling_host_session_id() or "").strip() == host_session_id:
                actor_id = str(
                    current_calling_agent_context_id(project_root) or ""
                ).strip()
        except Exception:
            actor_id = ""
    if not actor_id and host_kind:
        try:
            from .agent_memory_epoch import derive_agent_context_id

            actor_id = derive_agent_context_id(
                host_kind=host_kind,
                project_root=project_root,
                host_session_id=host_session_id,
            )
        except Exception:
            actor_id = ""
    if not session_id:
        try:
            from .managed_mode_service import (
                ManagedModeService,
                resolve_managed_session,
            )

            session_id = resolve_managed_session(
                ManagedModeService(),
                project_root,
                host_session_id=host_session_id,
            )
        except Exception:
            session_id = ""

    now = time.time()
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT INTO msg_role_map "
            "(host_session_id, role, actor_id, session_id, host_kind, "
            "updated_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(host_session_id) DO UPDATE SET "
            "role=excluded.role, "
            "source=excluded.source, "
            "actor_id=CASE WHEN excluded.actor_id != '' "
            "THEN excluded.actor_id ELSE msg_role_map.actor_id END, "
            "session_id=CASE WHEN excluded.session_id != '' "
            "THEN excluded.session_id ELSE msg_role_map.session_id END, "
            "host_kind=CASE WHEN excluded.host_kind != '' "
            "THEN excluded.host_kind ELSE msg_role_map.host_kind END, "
            "updated_at=excluded.updated_at",
            (
                host_session_id,
                role,
                actor_id,
                session_id,
                host_kind,
                now,
                MSG_SEAT_SOURCE_ENTER,
            ),
        )
        row = conn.execute(
            "SELECT role, actor_id, session_id, host_kind "
            "FROM msg_role_map WHERE host_session_id=?",
            (host_session_id,),
        ).fetchone()
        conn.commit()
    return {
        "host_session_id": host_session_id,
        "role": str(row["role"] or "") if row else role,
        "actor_id": str(row["actor_id"] or "") if row else actor_id,
        "session_id": str(row["session_id"] or "") if row else session_id,
        "host_kind": str(row["host_kind"] or "") if row else host_kind,
    }


def msg_resolve_caller_role(project_root: Path) -> str:
    """Resolve the calling agent's role — FAIL CLOSED (#215).

    Reads host_session_id from the current MCP context and looks it up in
    msg_role_map. A seat is MAPPED explicitly when it binds (the conductor via
    ai_seat(enter) → msg_register_role). An UNMAPPED caller — a lane worker, an
    unregistered agent, or a fresh project with an empty map — resolves to the
    non-privileged MSG_ROLE_UNMAPPED, NOT 'conductor'. Previously this defaulted
    to 'conductor', letting any unmapped caller drain the conductor's inbox and
    post as the conductor (seat-identity spoof). Identity is not a default.
    """
    try:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host_sid = (current_calling_host_session_id() or "").strip()
    except Exception:
        host_sid = ""
    if host_sid:
        try:
            with _connect(project_root) as conn:
                row = conn.execute(
                    "SELECT role FROM msg_role_map WHERE host_session_id = ?",
                    (host_sid,),
                ).fetchone()
                if row and row["role"] in MSG_ROLES:
                    return row["role"]
        except Exception:
            pass
    return MSG_ROLE_UNMAPPED


def msg_role_for_host(project_root: Path, host_session_id: str) -> str:
    """Resolve the role mapped to an EXPLICIT host_session_id — FAIL CLOSED.

    Same contract as msg_resolve_caller_role, minus the MCP-context lookup:
    hook subprocesses (claude_hook Stop/UPS) carry the host session id in the
    hook payload, not in an MCP call context, so they must pass it explicitly.
    Unmapped / empty / broken read → MSG_ROLE_UNMAPPED, never 'conductor'.
    """
    sid = str(host_session_id or "").strip()
    if not sid:
        return MSG_ROLE_UNMAPPED
    try:
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT role FROM msg_role_map WHERE host_session_id = ?",
                (sid,),
            ).fetchone()
            if row and row["role"] in MSG_ROLES:
                return row["role"]
    except Exception:
        pass
    return MSG_ROLE_UNMAPPED


# ── SEAT LIFECYCLE (#880, operator seat law 2026-08-24) ───────────────────
#
# Operator law, verbatim: "only 1 conductor and co-conductor can be active on
# an AIDOCS session, with any number of sub-agents (with parent-child tree-ing
# for responsibility ex: freeze/strikes)".
#
# `msg_role_map` had NO LIFECYCLE. A seat was TAKEN (`ai_seat enter` /
# `co-enter` -> `msg_register_role`) and NOTHING ever released one. Measured on
# the operator's box 2026-08-24: 9 rows, every one `role='conductor'`, TWO of
# them scoped to session `phoenix`, and rows going back to 2026-07-25. The law
# was violated in the data.
#
# THE LEASE TABLE HAD THIS EXACT GAP and it is closed the same way
# (`window_binding_store.reap_dead_windows`). The guards below are that
# function's guards, kept because each was learned by measurement.


def _seat_windows_for(project_root: Path, host_session_id: str) -> list[str]:
    """Which windows hold this conversation? The lease is the only answer.

    `msg_role_map` is keyed by host_session_id -- a CONVERSATION -- and a
    conversation has no pid. The window lease is the one structure that maps a
    conversation to a process, so it is the only route from a seat to a
    liveness question.
    """
    from .window_binding_store import WindowBindingStore

    return WindowBindingStore().conversation_windows(project_root, host_session_id)


def _seat_liveness(windows: list[str], checker) -> bool | None:
    """Grade ONE seat from its windows: ``True`` / ``False`` / ``None``.

    ``None`` -- UNPROVABLE, and it is the default. Three separate routes lead
    here and none of them is death:

      * NO WINDOW ROW AT ALL. This is the honest difference from the lease's
        own reaper, and it is not a detail. `conversation_windows` documents
        that absence from that table is evidence BECAUSE the table evicts
        nothing -- but it also only knows windows that fired SessionStart
        since it began recording (2026-08-23). Measured 2026-08-24: 8 of the
        9 live seats map to no window row, and every one of them was real.
        Grading that absence as death would have deleted eight real seats on
        the first run. So an empty list is UNPROVABLE here, deliberately, even
        though the lease reads its own absence differently.
      * A WINDOW WHOSE LIVENESS IS UNKNOWN -- a non-win32 host, an unreadable
        creation time. `_window_process_is_alive` returns None and it must not
        be collapsed into a denial: this feeds a DELETE.
      * A RAISE. An exception proves nothing.

    ``False`` only when the seat HAS windows and EVERY one is provably gone.
    ``True`` when any one of them is provably up -- one conversation
    legitimately appears on two windows at once (measured 2026-08-23), so one
    live holder is enough.
    """
    if not windows:
        return None
    verdicts: list[bool | None] = []
    for window in windows:
        pid_part, _, created_part = str(window or "").partition(":")
        try:
            pid = int(pid_part)
            created = int(created_part)
        except (TypeError, ValueError):
            verdicts.append(None)
            continue
        try:
            verdicts.append(checker(pid, created))
        except Exception:  # noqa: BLE001 -- a raise proves nothing
            verdicts.append(None)
    if any(v is True for v in verdicts):
        return True
    if verdicts and all(v is False for v in verdicts):
        return False
    return None


def msg_reap_dead_seats(
    project_root: Path,
    *,
    is_alive=None,
    windows_for=None,
) -> dict:
    """Release seats whose window is PROVABLY gone. Returns a report.

    POSITIVE PROOF ONLY. A row is deleted only on a ``False`` verdict from
    `_seat_liveness`. Everything else keeps its seat. Releasing on doubt would
    strip a live conductor of the seat it needs to conduct, and the seat cannot
    be re-taken by any call the stripped agent is still allowed to make -- the
    same lockout shape #880 items 3 and 4 spell out for the lease.

    BOTH HALVES, always: the default checker is the lease's
    `_window_process_is_alive`, which checks pid AND creation filetime. A live
    pid alone is not the same window -- Windows recycles pids, and a recycled
    pid whose creation time is unchecked lets a NEW process inherit a dead
    agent's SEAT, which since #215 is a privilege (post as the conductor, drain
    the conductor's inbox). That is the one-way door.

    THE PID-NAMESPACE GUARD. If NOT ONE seat can be confirmed alive, nothing is
    released. On the VPS gate the daemon shares no pid namespace with the
    windows, so its liveness answers are about unrelated processes; a reaper
    that believed them would clear every tenant's seat and look like it was
    working. "Wrong namespace" and "every window really is closed" are
    indistinguishable from here, so the safe reading is chosen -- the same
    completeness rule the lease reaper and #892's per-session classifier use.

    ``is_alive(pid, created_filetime)`` and ``windows_for(root, host_session)``
    are injected for tests; the defaults are the real lease.
    """
    checker = is_alive if is_alive is not None else _seat_window_is_alive
    lookup = windows_for if windows_for is not None else _seat_windows_for

    # NEVER ADOPT A FOLDER BY LOOKING AT IT. `_connect` does
    # `path.parent.mkdir(parents=True, exist_ok=True)`, so merely READING the
    # seat map CREATES `.MEMORY/` — and since this reaper runs on every
    # SessionStart, opening a Claude session in any unadopted directory would
    # silently adopt it. Measured by test_no_adoption_by_side_effect:
    # "SessionStart adopted an unadopted folder by creating .MEMORY".
    #
    # A store that was never created holds no seats to release, so the honest
    # answer is "nothing here", not a freshly minted empty table. This is the
    # same posture as the rest of the reaper: it reports what it can PROVE and
    # invents nothing -- including, now, the store itself.
    if not _db_path(project_root).exists():
        return {"released": [], "skipped": "no_seat_store"}

    try:
        with _connect(project_root) as conn:
            rows = conn.execute(
                "SELECT host_session_id, role, session_id, actor_id FROM msg_role_map",
            ).fetchall()
    except Exception:  # noqa: BLE001 -- an unreadable map releases nothing
        return {"released": [], "skipped": "seat_map_unreadable"}

    verdicts: list[tuple[str, str, str, str, bool | None]] = []
    any_alive = False
    for row in rows or []:
        host = str(row["host_session_id"] or "")
        try:
            windows = lookup(project_root, host)
        except Exception:  # noqa: BLE001 -- a raise proves nothing
            windows = None
        verdict = None if windows is None else _seat_liveness(list(windows), checker)
        if verdict is True:
            any_alive = True
        verdicts.append(
            (
                host,
                str(row["role"] or ""),
                str(row["session_id"] or ""),
                str(row["actor_id"] or ""),
                verdict,
            ),
        )

    if not any_alive:
        return {"released": [], "skipped": "no_seat_confirmed_alive"}

    released: list[dict] = []
    for host, role, session, actor, verdict in verdicts:
        if verdict is not False:
            continue
        try:
            with _connect(project_root) as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM msg_role_map WHERE host_session_id = ?",
                    (host,),
                )
                # ONE REAPER, BOTH STORES (DRT-10). This released the legacy
                # projection and left the AUTHORITATIVE `xaacp_actors` seat held
                # by the dead conversation -- so the window was proven gone, the
                # row was gone, and the seat still refused every successor
                # `occupied`. A second reaper for the authoritative half would be
                # a rival with its own death rule; this is the SAME positive
                # proof, applied to both halves in one transaction.
                #
                # SCOPED TO THE ACTOR THIS ROW NAMES. The clear fires only when
                # the authoritative seat still belongs to the same actor on the
                # same session -- never "whoever holds this role now". An
                # unattributed legacy row (blank actor or session, the pre-scope
                # migration's shape) names nobody, so it releases nothing here.
                if actor and session and role:
                    conn.execute(
                        "UPDATE xaacp_actors SET role='', updated_at=? "
                        "WHERE actor_id=? AND session_id=? AND role=?",
                        (time.time(), actor, session, role),
                    )
                conn.commit()
        except Exception:  # noqa: BLE001 -- a row we could not delete is not released
            continue
        released.append(
            {"host_session_id": host, "role": role, "session_id": session},
        )
    return {"released": released, "skipped": ""}


def reap_dead_seats_on_session_start(
    project_root: Path,
    *,
    is_alive=None,
    windows_for=None,
) -> dict:
    """Release provably-dead seats. Best-effort, NEVER raises.

    Called from SessionStart for the reason the lease reaper states for
    itself: the daemon is long-lived (measured ~18h uptime), so a boot-time
    reap on a process that does not restart is a reap that does not happen.

    ORDER MATTERS, AND IT IS THE OPPOSITE OF THE OBVIOUS ONE. This must run
    BEFORE `reap_dead_windows_on_session_start`. The seat reaper's ONLY route
    from a seat to a pid is the lease row; if the lease reaps that row first,
    every seat it would have graded DEAD becomes UNPROVABLE instead and the
    seat survives forever. The window reap would silently destroy the evidence
    the seat reap depends on.

    SESSIONSTART AVAILABILITY IS NOT NEGOTIABLE -- every failure degrades to
    "nothing released".
    """
    try:
        return msg_reap_dead_seats(
            project_root, is_alive=is_alive, windows_for=windows_for
        )
    except Exception as exc:  # noqa: BLE001
        try:
            logger.warning(
                "[aidocs seat] seat reap skipped: %s: %s", type(exc).__name__, exc
            )
        except Exception:
            pass
        return {"released": [], "skipped": "reaper_failed"}


def msg_inbox(
    project_root: Path,
    *,
    role: str,
    unread_only: bool = True,
    mark_read: bool = True,
) -> list[dict]:
    """Return messages addressed to `role`, oldest-first.

    Read-state is per-recipient (msg_reads table) so a broadcast
    drained by one role stays visible to the other targets until each
    has fetched it.
    """
    role = str(role or "").strip().lower()
    if role not in MSG_ROLES:
        raise ValueError(f"Invalid role '{role}'. Expected one of: {list(MSG_ROLES)}")
    out: list[dict] = []
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT id, from_role, to_roles_json, content, thread_id, "
            "       created_at "
            "FROM messages WHERE direction = 'msg' "
            "ORDER BY created_at",
        ).fetchall()
        read_ids = {
            r["message_id"]
            for r in conn.execute(
                "SELECT message_id FROM msg_reads WHERE role = ?",
                (role,),
            ).fetchall()
        }
        matched_ids: list[str] = []
        for r in rows:
            try:
                targets = json.loads(r["to_roles_json"] or "[]")
            except Exception:
                targets = []
            if role not in targets:
                continue
            already_read = r["id"] in read_ids
            if unread_only and already_read:
                continue
            matched_ids.append(r["id"])
            out.append(
                {
                    "id": r["id"],
                    "from_role": r["from_role"],
                    "to_roles": targets,
                    "body": r["content"],
                    "thread_id": r["thread_id"],
                    "created_at": r["created_at"],
                    "status": "read" if already_read else "pending",
                },
            )
        if mark_read and matched_ids:
            now = time.time()
            conn.executemany(
                "INSERT OR IGNORE INTO msg_reads (message_id, role, read_at) VALUES (?, ?, ?)",
                [(mid, role, now) for mid in matched_ids],
            )
            conn.commit()
    return out


def msg_thread(project_root: Path, *, thread_id: str) -> list[dict]:
    """Return the full message thread, ordered by created_at."""
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return []
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT id, from_role, to_roles_json, content, thread_id, "
            "       created_at, status "
            "FROM messages WHERE direction = 'msg' AND thread_id = ? "
            "ORDER BY created_at",
            (thread_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            targets = json.loads(r["to_roles_json"] or "[]")
        except Exception:
            targets = []
        out.append(
            {
                "id": r["id"],
                "from_role": r["from_role"],
                "to_roles": targets,
                "body": r["content"],
                "thread_id": r["thread_id"],
                "created_at": r["created_at"],
                "status": r["status"],
            },
        )
    return out


def msg_format_block(messages: list[dict]) -> str:
    """Format pending messages for UPS-hook surface. Mirrors
    run_notifications.format_block — leads with 📨, oldest-first.
    """
    if not messages:
        return ""
    lines = ["📨 Messages:"]
    for m in messages:
        targets = ",".join(m.get("to_roles") or [])
        lines.append(
            f"  • [{m.get('id', '')}] {m.get('from_role', '?')} → {targets}: "
            f"{(m.get('body', '') or '').strip()}",
        )
    lines.append("Reply with msg_reply(message_id, body) or msg_send(...).")
    return "\n".join(lines)


def _xaacp_rendered_stamp(created_at: object) -> str:
    """" When was this AUTHORED?" -- rendered, not merely stored.

    The rail used to print id/sender/target/kind/body and NOTHING ELSE, so a
    reader could not tell an hour-old message from a fresh one. Measured
    2026-09-10: the conductor received a Consul's review stapled to an
    unrelated tool result and answered it as though it replied to the message
    it had JUST sent -- it did not; it predated it. `created_at` was on the row
    the whole time. The store had the fact and the SURFACE DROPPED IT, which is
    this codebase's most-repeated defect shape.

    Fail-OPEN to the empty string on any bad value: this runs on the
    notification rail, so raising here would cost every actor its delivery to
    save a timestamp. An absent stamp is a missing convenience; a raised
    exception is a silenced rail.
    """
    try:
        secs = float(created_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(secs))
    except (ValueError, OSError, OverflowError):
        return ""


def xaacp_format_block(messages: list[dict]) -> str:
    """Format actor-routed messages for the universal next-tool delivery rail."""
    if not messages:
        return ""
    lines = ["📨 XAACP messages:"]
    incoming = [m for m in messages if m.get("entry_kind", "incoming") != "outcome"]
    for m in messages:
        if m.get("entry_kind") == "outcome":
            # #1059: the other side's answer to something I SENT, rendered on
            # its thread, so an agent never needs to know where replies live.
            state = str(m.get("outcome_state") or m.get("status") or "")
            parts = [f"  • [{m.get('id', '')}]"]
            stamp = _xaacp_rendered_stamp(m.get("state_at"))
            if stamp:
                parts.append(stamp)
            parts.append(
                f"outcome from {m.get('target_actor_id', '?')} on your "
                f"{m.get('message_kind', 'message')} (thread {m.get('thread_id', '')}): {state}"
            )
            if m.get("ack_body"):
                parts.append(f"| ack: {str(m['ack_body']).strip()}")
            if state != "acked" and m.get("response"):
                parts.append(f"| response: {str(m['response']).strip()}")
            lines.append(" ".join(parts))
            continue
        # AUTHORING TIME and CAUSAL PARENT, both rendered (#1057). Delivery
        # order is NOT authoring order on this rail -- the rail surfaces
        # whatever is outstanding on the next tool call, so a reader with only
        # a body cannot place a message in the conversation. `in_reply_to` is
        # the stronger of the two: two Consuls on different hosts share no
        # clock, so the reply chain -- not wall-clock -- is what establishes
        # what answers what. Both are omitted when absent rather than printed
        # as "?" or "None", because an empty field reads as unknown while a
        # fabricated one reads as fact.
        stamp = _xaacp_rendered_stamp(m.get("created_at"))
        parts = [f"  • [{m.get('id', '')}]"]
        if stamp:
            parts.append(stamp)
        parts.append(
            f"{m.get('sender_actor_id', '?')} → {m.get('target_actor_id', '?')}"
        )
        kind = m.get("message_kind", "message")
        reply_to = str(m.get("reply_to_id") or m.get("in_reply_to") or "").strip()
        parts.append(f"({kind}, re: {reply_to}):" if reply_to else f"({kind}):")
        lines.append(
            " ".join(parts) + f" {(m.get('body', '') or '').strip()}"
        )
    if not incoming:
        return "\n".join(lines)
    # DRT-12: the ACK is offered FIRST because it is the cheap honest answer.
    # Naming only xaacp_reply pushed an agent that had merely received the
    # message into picking a TERMINAL decision for it.
    lines.append("Acknowledge receipt with ai_msg(mode='xaacp_ack', message_id=..., session_id=..., body=...) -- this does NOT decide it.")
    lines.append("Reply with ai_msg(mode='xaacp_reply', message_id=..., session_id=..., decision=..., body=...).")
    return "\n".join(lines)


# ── Read Gate (#217) ──
# Unread seat messages / unread lane mailbox prompts are BLOCKERS: the
# addressee may not run other tools until the message is read/consumed.
# These are the STORE + CHECK primitives; the admit-time enforcement
# hook lives in the tool-gate layer (host-agnostic UPS path) and calls
# read_gate_check() per tool call. Fail-safe: past TTL a stale message
# auto-clears the block with an audit line, so a stuck/oversized
# message can never permanently brick the agent.

# Mirror of LaneMailboxStore.DEFAULT_TTL_SECONDS (#217 fail-safe).
MSG_READ_GATE_TTL_SECONDS = 15 * 60

# The drain/bind surfaces must never be blocked by the gate, or the
# agent could not read the very message that blocks it. ADDITIVE-only:
# read/drain/bind tools only — never mutation tools.
MSG_GATE_EXEMPT_TOOLS = frozenset(
    {
        "ai_msg",
        "ai_session",
        "session_connect",
        "ai_lane_inbox",
        "ai_notifications_clear",
        "ai_gate_msg",
    },
)


def _normalize_tool_name(tool_name: str) -> str:
    """Strip host MCP prefixes (mcp__aidocs__ai_msg → ai_msg)."""
    name = str(tool_name or "").strip()
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[-1]
    return name


def _audit_event(
    project_root: Path,
    session_id: str,
    *,
    action_kind: str,
    target_entity: str,
    payload: dict,
    event_kind: str = "conductor_comms",
) -> None:
    """Best-effort write to execution_events. Never raises."""
    try:
        from .execution_index_store import ExecutionIndexStore

        if event_kind == "conductor_comms":
            ExecutionIndexStore().record_event(
                project_root,
                event_kind="conductor_comms",
                source_kind="mcp",
                session_id=session_id,
                action_kind=action_kind,
                target_entity=target_entity,
                status="ok",
                payload=payload,
            )
            return
        ExecutionIndexStore().record_event(
            project_root,
            event_kind=event_kind,
            source_kind="mcp",
            session_id=session_id,
            action_kind=action_kind,
            target_entity=target_entity,
            status="ok",
            payload=payload,
        )
    except Exception:
        pass


def msg_read_gate_check(
    project_root: Path,
    *,
    role: str,
    tool_name: str = "",
    ttl_seconds: int | None = None,
) -> dict:
    """Admit-time read-gate for seat messages (#217).

    Returns {"blocked": bool, ...}. BLOCKED when `role` has unread
    role-addressed messages younger than the TTL and `tool_name` is not
    an exempt drain surface. Non-consuming: only an explicit inbox
    drain (msg_inbox / ai_msg) clears the block — the check itself
    never marks a live message read. Messages older than the TTL are
    expired FOR THIS ROLE (msg_reads row + audit line) so a stale
    message cannot brick the agent forever.

    Fail-open only for callers with no seat: the msg gate blocks seat
    inboxes; identity itself is gated fail-closed elsewhere (#215).
    """
    role = str(role or "").strip().lower()
    if role not in MSG_ROLES:
        return {"blocked": False, "reason": "no_seat", "role": role}
    if _normalize_tool_name(tool_name) in MSG_GATE_EXEMPT_TOOLS:
        return {"blocked": False, "reason": "exempt_tool", "role": role}

    ttl = ttl_seconds if ttl_seconds is not None else MSG_READ_GATE_TTL_SECONDS
    cutoff = time.time() - ttl
    live: list[dict] = []
    expired_ids: list[str] = []
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT id, to_roles_json, created_at FROM messages "
            "WHERE direction = 'msg' ORDER BY created_at",
        ).fetchall()
        read_ids = {
            r["message_id"]
            for r in conn.execute(
                "SELECT message_id FROM msg_reads WHERE role = ?",
                (role,),
            ).fetchall()
        }
        for r in rows:
            try:
                targets = json.loads(r["to_roles_json"] or "[]")
            except Exception:
                targets = []
            if role not in targets or r["id"] in read_ids:
                continue
            if float(r["created_at"] or 0) < cutoff:
                expired_ids.append(r["id"])
            else:
                live.append({"id": r["id"], "created_at": r["created_at"]})
        if expired_ids:
            now = time.time()
            conn.executemany(
                "INSERT OR IGNORE INTO msg_reads (message_id, role, read_at) "
                "VALUES (?, ?, ?)",
                [(mid, role, now) for mid in expired_ids],
            )
            conn.commit()
    if expired_ids:
        _audit_event(
            project_root,
            "",
            action_kind="msg_read_gate_expire",
            target_entity=role,
            payload={
                "expired_count": len(expired_ids),
                "message_ids": expired_ids,
                "ttl_seconds": ttl,
            },
        )
    if live:
        return {
            "blocked": True,
            "blocked_by": "unread_messages",
            "role": role,
            "unread_count": len(live),
            "oldest_id": live[0]["id"],
            "expired_count": len(expired_ids),
            "refusal": (
                f"BLOCKED: {len(live)} unread message(s) addressed to your "
                f"seat '{role}'. Read your inbox first: ai_msg(mode='inbox') "
                f"— no other action is permitted until the inbox is drained."
            ),
        }
    return {
        "blocked": False,
        "role": role,
        "expired_count": len(expired_ids),
    }


def lane_read_gate_check(
    project_root: Path,
    *,
    worker_id: str,
    tool_name: str = "",
) -> dict:
    """Admit-time read-gate for the lane mailbox (#217).

    BLOCKED when `worker_id` has a pending (non-expired) mailbox prompt
    and `tool_name` is not an exempt drain/bind surface. Reuses the
    mailbox's own TTL sweep as the fail-safe: stale rows flip to
    'expired' (with the store's audit line) and stop blocking.
    """
    wid = str(worker_id or "").strip()
    if not wid:
        return {"blocked": False, "reason": "no_worker_id"}
    if _normalize_tool_name(tool_name) in MSG_GATE_EXEMPT_TOOLS:
        return {"blocked": False, "reason": "exempt_tool"}
    from .lane_mailbox_store import LaneMailboxStore

    store = LaneMailboxStore()
    store.expire_stale(project_root)
    pending = store.peek(project_root, worker_id=wid)
    if pending is None:
        return {"blocked": False, "worker_id": wid}
    return {
        "blocked": True,
        "blocked_by": "unread_lane_mailbox",
        "worker_id": wid,
        "mailbox_id": pending["mailbox_id"],
        "refusal": (
            f"BLOCKED: an unread lane mailbox prompt (mailbox_id="
            f"{pending['mailbox_id']}) is pending for worker '{wid}'. "
            f"Consume it first — it is injected on your next turn "
            f"(session_connect / ai_lane_inbox drains it); no other "
            f"action is permitted until then."
        ),
    }


def read_gate_check(
    project_root: Path,
    *,
    role: str = "",
    worker_id: str = "",
    tool_name: str = "",
) -> dict:
    """Combined #217 blocker check — seat inbox first, then lane
    mailbox. One call for the admit-time gate.
    """
    if role:
        seat = msg_read_gate_check(project_root, role=role, tool_name=tool_name)
        if seat["blocked"]:
            return seat
    if worker_id:
        lane = lane_read_gate_check(
            project_root, worker_id=worker_id, tool_name=tool_name
        )
        if lane["blocked"]:
            return lane
    return {"blocked": False}


# ── Lane Scope Ask/Grant (#218) ──
# Worker-asks-conductor -> conductor-grants. A lane worker that hits a
# scope wall submits a STRUCTURED ask (it may not self-expand — that
# would be self-escalation); a seat role grants (additive-only, subset
# of the ask) or denies. Delivery: the grant extends lane_scopes AND
# queues a lane-mailbox resume prompt, so it reaches the worker via
# grant+resume today and via live per-worker rows after the
# session-lane-agents-table migration.


def lane_scope_ask(
    project_root: Path,
    *,
    lane_id: str,
    requested_paths: list[str],
    reason: str = "",
    session_id: str = "",
    worker_id: str = "",
    kind: str = "files",
) -> dict:
    """Worker submits a structured scope-expansion ask. BLOCKING per
    #217 — the pending ask is answered by the conductor, not by the
    worker, and the worker waits rather than self-serving.

    WAR D (#452/#218): ``kind`` ∈ {'files', 'tools'} — the same ask
    channel carries request_scope_extension for tool grants; items
    ride in ``requested_paths`` either way (tool names are not
    slash-normalized).
    """
    kind = str(kind or "files").strip().lower()
    if kind not in ("files", "tools"):
        raise ValueError(f"kind must be 'files' or 'tools', got {kind!r}")
    if kind == "files":
        normalized = [
            p.replace("\\", "/").strip()
            for p in (requested_paths or [])
            if str(p).strip()
        ]
    else:
        normalized = [str(p).strip() for p in (requested_paths or []) if str(p).strip()]
    if not normalized:
        raise ValueError("requested_paths must contain at least one item")
    msg_id = str(uuid4())[:12]
    content = json.dumps(
        {
            "requested_paths": normalized,
            "reason": str(reason or ""),
            "worker_id": str(worker_id or ""),
            "kind": kind,
        },
    )
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT INTO messages (id, lane_id, session_id, direction, "
            "category, content, status, created_at) "
            "VALUES (?, ?, ?, 'agent_to_conductor', 'scope_request', ?, "
            "'pending', ?)",
            (msg_id, lane_id, session_id, content, time.time()),
        )
        conn.commit()
    _audit_event(
        project_root,
        session_id,
        action_kind="lane_scope_ask",
        target_entity=lane_id,
        payload={
            "ask_id": msg_id,
            "requested_paths": normalized,
            "worker_id": worker_id,
            "kind": kind,
        },
    )
    return {
        "id": msg_id,
        "status": "pending",
        "lane_id": lane_id,
        "requested_paths": normalized,
        "kind": kind,
    }


def get_pending_scope_asks(project_root: Path, session_id: str = "") -> list[dict]:
    """Conductor-visible list of pending structured scope asks."""
    with _connect(project_root) as conn:
        if session_id:
            rows = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' "
                "AND category = 'scope_request' AND status = 'pending' "
                "AND session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' "
                "AND category = 'scope_request' AND status = 'pending' "
                "ORDER BY created_at",
            ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            body = json.loads(r["content"])
        except Exception:
            continue  # legacy free-text scope_request — not structured
        if not isinstance(body, dict) or "requested_paths" not in body:
            continue
        out.append(
            {
                "id": r["id"],
                "lane_id": r["lane_id"],
                "session_id": r["session_id"],
                "requested_paths": list(body.get("requested_paths") or []),
                "reason": str(body.get("reason") or ""),
                "worker_id": str(body.get("worker_id") or ""),
                "kind": str(body.get("kind") or "files"),
                "created_at": r["created_at"],
            },
        )
    return out


def lane_scope_grant(
    project_root: Path,
    ask_id: str,
    *,
    granter_role: str,
    grant: bool = True,
    granted_paths: list[str] | None = None,
    reason: str = "",
) -> dict:
    """Seat-only resolution of a structured scope ask (#218).

    Fail-closed: only a seat role (MSG_ROLES) may grant — a worker
    granting its own ask is self-escalation and is refused. Grants are
    additive-only and must be a subset of the requested paths (a grant
    can narrow the ask, never smuggle extra scope). On grant:
    lane_scopes is extended, the gate's known_exact_paths gets the
    paths (best-effort), the ask is answered, and a resume prompt is
    queued in the worker's lane mailbox for delivery.
    """
    granter = str(granter_role or "").strip().lower()
    if granter not in MSG_ROLES:
        return {
            "granted": False,
            "blocked_by": "not_a_seat",
            "reason": (
                f"granter_role '{granter_role}' is not a seat "
                f"({list(MSG_ROLES)}); workers may not grant their own "
                f"scope — route the ask to the conductor."
            ),
            "ask_id": ask_id,
        }
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id = ? AND category = 'scope_request'",
            (ask_id,),
        ).fetchone()
    if row is None:
        return {"granted": False, "blocked_by": "ask_not_found", "ask_id": ask_id}
    if row["status"] != "pending":
        return {
            "granted": False,
            "blocked_by": "already_resolved",
            "ask_id": ask_id,
            "status": row["status"],
        }
    try:
        body = json.loads(row["content"])
        requested = [str(p) for p in (body.get("requested_paths") or [])]
    except Exception:
        return {"granted": False, "blocked_by": "malformed_ask", "ask_id": ask_id}
    lane_id = row["lane_id"]
    session_id = row["session_id"]
    worker_id = str(body.get("worker_id") or "")

    if not grant:
        answer_question(
            project_root,
            ask_id,
            f"SCOPE DENIED by {granter}: {reason or 'no reason given'}",
        )
        _audit_event(
            project_root,
            session_id,
            action_kind="lane_scope_deny",
            target_entity=lane_id,
            payload={"ask_id": ask_id, "granted_by": granter, "reason": reason},
        )
        return {
            "granted": False,
            "denied": True,
            "ask_id": ask_id,
            "lane_id": lane_id,
            "granted_by": granter,
        }

    to_grant = [
        p.replace("\\", "/").strip()
        for p in (granted_paths if granted_paths is not None else requested)
        if str(p).strip()
    ]
    outside = [p for p in to_grant if p not in requested]
    if outside:
        # Refuse WITHOUT consuming the ask — a corrected grant may follow.
        return {
            "granted": False,
            "blocked_by": "paths_outside_ask",
            "ask_id": ask_id,
            "outside_paths": outside,
            "reason": "granted_paths must be a subset of the requested paths",
        }
    if not to_grant:
        return {
            "granted": False,
            "blocked_by": "empty_grant",
            "ask_id": ask_id,
            "reason": "nothing to grant — use grant=False to deny",
        }

    # Additive-only scope extension for this lane.
    with _connect(project_root) as conn:
        for p in to_grant:
            conn.execute(
                "INSERT OR IGNORE INTO lane_scopes "
                "(lane_id, session_id, file_path) VALUES (?, ?, ?)",
                (lane_id, session_id, p),
            )
        conn.commit()

    # Best-effort: unblock the read gate's known_exact_paths too.
    try:
        from .query_gate import QueryGateStore

        store = QueryGateStore()
        state = store.get(project_root, session_id)
        known = list(state.get("known_exact_paths") or [])
        changed = False
        for p in to_grant:
            if p not in known:
                known.append(p)
                changed = True
        if changed:
            store.set(project_root, session_id, known_exact_paths=known)
    except Exception:
        pass

    answer_question(
        project_root,
        ask_id,
        f"SCOPE GRANTED by {granter}: {', '.join(to_grant)} added to lane "
        f"'{lane_id}' scope.",
    )

    # Delivery: env scope is frozen at spawn, so land a resume prompt.
    if worker_id:
        try:
            from .lane_mailbox_store import LaneMailboxStore

            LaneMailboxStore().put(
                project_root,
                worker_id=worker_id,
                session_id=session_id,
                prompt=(
                    f"SCOPE GRANTED by {granter}: {', '.join(to_grant)} "
                    f"added to lane '{lane_id}' scope (ask {ask_id}). "
                    f"Resume your task with the expanded scope."
                ),
                author_session_id=session_id or None,
            )
        except Exception:
            pass

    _audit_event(
        project_root,
        session_id,
        action_kind="lane_scope_grant",
        target_entity=lane_id,
        payload={
            "ask_id": ask_id,
            "granted_paths": to_grant,
            "granted_by": granter,
            "worker_id": worker_id,
        },
    )
    return {
        "granted": True,
        "ask_id": ask_id,
        "lane_id": lane_id,
        "granted_paths": to_grant,
        "granted_by": granter,
        "worker_id": worker_id,
    }

def lane_grant_scope(
    project_root: Path,
    *,
    session_id: str,
    lane_id: str,
    kind: str,
    items: list[str],
    granted_by: str = "conductor",
    reason: str = "",
) -> dict:
    """WAR D (#452/#218): conductor-side additive widening of a lane's
    query-gate columns — the SAME columns the spawn path stamps.

    kind='files' → lane_exact_paths (+ lane_scopes registration for
    cross-lane conflict detection); kind='tools' → lane_extra_tools
    (EXTENSION of the declared toolset, never a replacement). Deny is
    a reply message with no state change — callers that deny simply
    do not call this. Audited as lane_grant_scope with attribution.
    """
    kind = str(kind or "").strip().lower()
    if kind not in ("files", "tools"):
        return {
            "granted": False,
            "blocked_by": "invalid_kind",
            "reason": f"kind must be 'files' or 'tools', got {kind!r}",
        }
    if kind == "files":
        clean = [str(p).replace("\\", "/").strip() for p in (items or []) if str(p).strip()]
    else:
        clean = [str(p).strip() for p in (items or []) if str(p).strip()]
    if not clean:
        return {
            "granted": False,
            "blocked_by": "empty_grant",
            "reason": "items must contain at least one entry",
        }
    if not str(lane_id or "").strip():
        return {"granted": False, "blocked_by": "missing_lane_id", "reason": "lane_id required"}

    from .query_gate import QueryGateStore

    store = QueryGateStore()
    state = store.get(project_root, session_id)
    if kind == "files":
        current = list(state.get("lane_exact_paths") or [])
        added = [p for p in clean if p not in current]
        if added:
            store.set(
                project_root,
                session_id,
                lane_exact_paths=[*current, *added],
            )
        # Keep the cross-lane conflict registry in sync (additive).
        with _connect(project_root) as conn:
            for p in clean:
                conn.execute(
                    "INSERT OR IGNORE INTO lane_scopes "
                    "(lane_id, session_id, file_path) VALUES (?, ?, ?)",
                    (lane_id, session_id, p),
                )
            conn.commit()
        column = "lane_exact_paths"
    else:
        current = list(state.get("lane_extra_tools") or [])
        added = [t for t in clean if t not in current]
        if added:
            store.set(
                project_root,
                session_id,
                lane_extra_tools=[*current, *added],
            )
        column = "lane_extra_tools"

    _audit_event(
        project_root,
        session_id,
        action_kind="lane_grant_scope",
        target_entity=lane_id,
        payload={
            "kind": kind,
            "items": clean,
            "added": added,
            "column": column,
            "granted_by": granted_by,
            "reason": str(reason or ""),
        },
    )
    return {
        "granted": True,
        "lane_id": lane_id,
        "session_id": session_id,
        "kind": kind,
        "items": clean,
        "added": added,
        "column": column,
    }


_XAACP_PROTOCOL = "xaacp/1"
_XAACP_DECISIONS = frozenset({"accepted", "rejected", "completed", "blocked"})
_XAACP_TERMINAL = frozenset({*_XAACP_DECISIONS, "canceled", "expired"})
#: DRT-12 — ACK IS DELIBERATELY ABSENT FROM BOTH SETS ABOVE. Every member of
#: `_XAACP_DECISIONS` is terminal, which is the whole defect: `accepted` could
#: not mean "received, I own this", because the same message could then never
#: reach `completed` or `blocked`. Acknowledgement is therefore NOT a decision
#: and NOT a status -- it is its own non-terminal fact in its own columns
#: (`acked_at` / `acked_by` / `ack_body`). Adding "acked" to either set would
#: re-conflate exactly the three facts this separates:
#:
#:     delivered/read  !=  acknowledged responsibility  !=  eventual outcome
#:
#: The status returned by the ack surface, so nothing has to re-type it.
XAACP_ACK_STATUS = "acked"

# ── ONE DELIVERY TRUTH ──
#
# "IS THIS MESSAGE STILL OUTSTANDING FOR ITS ADDRESSEE" had THREE rival
# definitions over one table, and they disagreed about whether a message
# existed:
#
#   xaacp_inbox(unread_only=True)   no `msg_reads` row  (read flag)
#   xaacp_wait_next(after_cursor=0) no `msg_reads` row, then baseline the
#                                   cursor to MAX(rowid) if none was found
#   xaacp_reply                     `decision_status or status` not terminal
#
# The notification rail (notification_injector.py) is a REPORTER: it surfaces
# routed traffic on an unrelated tool result as the portable delivery floor.
# It read the inbox with `mark_read=True`, so merely ANNOUNCING a message
# stamped the read flag -- and the two surfaces whose whole job is delivery
# both went quiet, while `xaacp_reply` still answered it. Worse, wait_next
# then baselined its durable cursor PAST the message's rowid, so the delivery
# became permanently unreachable for that waiter. A reporting surface had
# consumed a delivery.
#
# This is the DRT-12 defect class on the transport side: READ-TRACKING AND
# DECISION-TRACKING ARE NOT THE SAME FACT. A message that has been seen is
# not a message that has been answered.
#
# So delivery keys off OUTSTANDING, defined once, here, and derived from
# `_XAACP_TERMINAL` so a new terminal state can never be forgotten by one
# surface and honoured by another. `msg_reads` survives as what it actually
# is -- an audit trail of who looked -- and no longer decides visibility.
_XAACP_OUTSTANDING_SQL = (
    "COALESCE(NULLIF(m.decision_status, ''), NULLIF(m.status, ''), 'pending') "
    "NOT IN (" + ",".join("?" * len(_XAACP_TERMINAL)) + ")"
)
#: Bind params for :data:`_XAACP_OUTSTANDING_SQL`. Sorted so the SQL text is
#: stable across processes (statement-cache friendly, and diffable in a log).
_XAACP_OUTSTANDING_PARAMS: tuple[str, ...] = tuple(sorted(_XAACP_TERMINAL))


# `xaacp_is_outstanding` USED TO LIVE HERE as "the Python twin of
# _XAACP_OUTSTANDING_SQL", justified as stopping a row-holding caller from
# re-deriving the rule. It was removed 2026-09-12 because it had ZERO
# references of any kind -- not one production caller, not one test -- so it
# prevented nothing. A twin nobody consults is not a second definition being
# kept honest; it is a second definition free to drift.
#
# The rule still has exactly one home per question it answers:
#   * the ROW-level question (COALESCE over decision_status / status /
#     'pending') is `_XAACP_OUTSTANDING_SQL`, used at the three query sites;
#   * the SCALAR question ("is this one already-resolved value terminal?") is
#     `_XAACP_TERMINAL` membership, used directly at the four call sites below.
# Both read the SAME frozenset, which is what makes a new terminal state
# impossible for one of them to forget.


_XAACP_MODES = frozenset({
    "xaacp_send", "xaacp_inbox", "xaacp_reply", "xaacp_wait", "xaacp_cancel",
    "xaacp_directory", "xaacp_ack", "wait_next",
})


def _xaacp_current_host_session_id() -> str:
    try:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        return str(current_calling_host_session_id() or "").strip()
    except Exception:
        return ""


def _xaacp_role_route_for_host(
    project_root: Path,
    host_session_id: str,
) -> dict[str, str]:
    """Resolve one seat actor's XAACP route -- IF that seat applies here.

    THE SEAT ROW IS SCOPE, NEVER THE CALLER'S SESSION (#880 phase 5, fixed
    2026-09-02). Until then this read the caller's work session STRAIGHT OFF
    ``msg_role_map.session_id`` and consulted the canonical per-conductor
    binding only when that column was EMPTY. A non-empty STALE scope was
    therefore never re-checked, and it outranked an explicit
    ``ai_session(connect)`` for as long as the row existed:

        seat entered on phoenix  ->  msg_role_map[H].session_id = phoenix
        connect H -> ubermega    ->  canonical binding is ubermega
        XAACP caller route       ->  still answered phoenix, forever

    MEASURED 2026-09-01: ``ai_whoami`` reported ``ubermega`` while ``ai_msg``
    refused with ``bound_session_id: phoenix`` -- and ``ai_seat(enter)``, the
    one tool that rewrites this very row, was itself refused by the stale row
    it would have rewritten. The remedy sat downstream of the defect.

    SO THE SESSION NOW COMES FROM THE ONE AUTHORITY, unconditionally, exactly
    as the unseated sibling ``_xaacp_bound_agent_route`` already resolves it.
    The row is then asked only what it is entitled to answer -- WHICH SESSION
    THIS SEAT IS FOR -- through ``seat_scope_matches``, whose docstring
    already stated this rule for two earlier readers that threw the scope
    away. This one did the inverse; same column, third misreading.

    A SCOPED SEAT IS NEVER MOVED. When the caller is on another session the
    seat simply does not apply: the caller is UNSEATED there (falling through
    to the bound-agent route) and may take a seat explicitly. Silently
    re-pointing the row would make a phoenix conductor materialise in
    ubermega without anyone ever entering that seat.

    A BLANK SCOPE IS NOT A SEAT, and this function no longer treats it as one.
    Legacy ``cerberus_role_map`` rows copied ``(host_session_id, role)`` and
    nothing else; ``seat_scope_matches`` calls them "a CONDUCTOR OF NO SESSION"
    that "cannot answer a question they were never scoped to answer". The first
    cut of this fix let such a row adopt the caller's session and stamped it
    back -- READ-TIME AUTHORITY CREATION, a read that mints a seat nobody
    entered. Those rows stay stored and stay UNSEATED; only
    ``ai_seat(enter/co-enter)`` scopes a seat. THIS FUNCTION NEVER WRITES THAT
    COLUMN.
    """
    hsid = str(host_session_id or "").strip()
    if not hsid:
        return {}
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT host_session_id, role, actor_id, session_id, host_kind "
            "FROM msg_role_map WHERE host_session_id = ?",
            (hsid,),
        ).fetchone()
    if row is None or str(row["role"] or "") not in MSG_ROLES:
        return {}

    role = str(row["role"] or "").strip()
    actor_id = str(row["actor_id"] or "").strip()
    # NOT `session_id`. Naming it for what it is was half the bug: the column
    # is the seat's scope, and calling it the session invited every reader to
    # treat it as the caller's.
    seat_scope = str(row["session_id"] or "").strip()
    host_kind = str(row["host_kind"] or "").strip()
    if not host_kind:
        try:
            from .mcp_server_runtime_helpers import current_calling_host_kind

            host_kind = str(current_calling_host_kind() or "").strip()
        except Exception:
            host_kind = ""
    if not actor_id:
        try:
            from .mcp_server_runtime_helpers import current_calling_agent_context_id

            actor_id = str(
                current_calling_agent_context_id(project_root) or ""
            ).strip()
        except Exception:
            actor_id = ""
    if not actor_id and host_kind:
        try:
            from .agent_memory_epoch import derive_agent_context_id

            actor_id = derive_agent_context_id(
                host_kind=host_kind,
                project_root=project_root,
                host_session_id=hsid,
            )
        except Exception:
            actor_id = ""

    # THE CALLER'S SESSION FROM THE MANAGED BINDING -- always, never from the
    # row. An unresolvable binding stays unresolved: identity has no fallback
    # (operator law 2026-08-23), so a stranger resolves to nothing rather than
    # to whatever a seat row happens to remember.
    #
    # THE ONE CANONICAL AUTHORITY, since #1001 (Landing 2 of #880, 2026-09-03).
    # For a known host id `get_mode` resolves the per-conductor row and
    # nothing else: the `_heal_chain_attested_binding` path that could
    # manufacture a missing row from the project singleton is retired, so a
    # caller with no row resolves to nothing here and the dispatcher names
    # the missing binding and ai_session(mode='connect') as the remedy.
    session_id = ""
    try:
        from .managed_mode_service import ManagedModeService, resolve_managed_session

        session_id = resolve_managed_session(
            ManagedModeService(),
            project_root,
            host_session_id=hsid,
        )
    except Exception:
        logger.exception("XAACP seat session resolution failed")
        return {}

    # A SEAT MAY CONFIRM A BINDING, NEVER SUPPLY ONE (#1014, law
    # promoted-cc6c4ac686ee: "no layer may invent, inherit, heal, or
    # substitute identity from a broader layer"). fefb1f96f kept a second arm
    # here -- "binding absent -> an explicitly entered seat answers for its
    # own scope" -- to keep four suites green whose hosts never ran
    # ai_session(connect). That arm read `msg_role_map.session_id` AS the
    # caller's session: a substitution. A seat is a ROLE fact about a
    # session; a caller with no managed binding has selected NO session, and
    # nothing a role row remembers can select one for it. Those suites now
    # write the binding in their setup, which is what a real host does.
    #
    # So there is exactly ONE question left for the row: does this seat's
    # scope match the session the binding proves? No binding -> {} and the
    # dispatcher names the missing binding and ai_session(mode='connect').
    # Mismatch -> {} and the caller is UNSEATED there (bound-agent route,
    # #732). A BLANK SCOPE SEATS NOBODY: `seat_scope_matches` refuses it, so
    # a legacy `cerberus_role_map` row stays stored and stays unseated.
    if not session_id or not seat_scope_matches(seat_scope, session_id):
        return {}

    # The scope is now provably EQUAL to session_id, so the UPDATE below cannot
    # move it -- it only heals actor_id / host_kind. There is deliberately no
    # branch that writes a scope this function derived.
    if actor_id and (
        actor_id != str(row["actor_id"] or "").strip()
        or host_kind != str(row["host_kind"] or "").strip()
    ):
        with _connect(project_root) as conn:
            conn.execute(
                "UPDATE msg_role_map SET actor_id=?, session_id=?, host_kind=?, "
                "updated_at=? WHERE host_session_id=?",
                (actor_id, session_id, host_kind, time.time(), hsid),
            )
            conn.commit()
    if not actor_id:
        return {}
    return {
        "actor_id": actor_id,
        "session_id": session_id,
        "lane_id": "",
        "actor_kind": "seat",
        "role": role,
        "host_session_id": hsid,
    }


def _xaacp_bound_agent_route(
    project_root: Path,
    host_session_id: str,
) -> dict[str, str]:
    """Route for a BOUND caller that holds no seat (#732).

    THE CLOSED LOOP THIS OPENS. `msg_send` refuses an unseated caller and names
    `ai_msg(mode='xaacp_send', ...)` as the way to reach its own conductor. That
    call resolved non-worker callers through `_xaacp_role_route_for_host`, which
    returns {} with no `msg_role_map` row -- and `msg_role_map` is written by
    exactly one thing, ai_seat(mode='enter'), which the SAME refusal explicitly
    forbids ("taking it would evict the sitting conductor"). Escape hatch and
    locked door, one key, and the caller told not to pick it up. Law 311bf3e6.

    NO AUTHORITY IS CONFERRED, which is why this is safe:
      * `role` stays EMPTY and actor_kind is 'agent', never 'seat' -- an
        unseated caller still cannot post AS a seat, and that refusal is correct
        and untouched. xaacp_directory documents this shape itself: "handles
        only, no authority conferred".
      * the actor id is DERIVED from the caller's own host session, never
        supplied by the caller, so no actor can be impersonated.
      * an ACTIVE managed-mode binding is required, so a stranger with no
        canonical session still resolves to nothing and stays fail-closed.

    `_xaacp_role_route_for_host` already derives both halves this way; it just
    discarded them when the row was missing instead of when the IDENTITY was.
    """
    hsid = str(host_session_id or "").strip()
    if not hsid:
        return {}
    session_id = ""
    try:
        from .managed_mode_service import ManagedModeService, resolve_managed_session

        session_id = resolve_managed_session(
            ManagedModeService(), project_root, host_session_id=hsid
        )
    except Exception:
        logger.exception("XAACP bound-agent session resolution failed")
        return {}
    if not session_id:
        return {}
    actor_id = ""
    try:
        from .mcp_server_runtime_helpers import current_calling_agent_context_id
        actor_id = str(current_calling_agent_context_id(project_root) or "").strip()
    except Exception:
        actor_id = ""
    if not actor_id:
        try:
            from .agent_memory_epoch import derive_agent_context_id
            from .mcp_server_runtime_helpers import current_calling_host_kind

            host_kind = str(current_calling_host_kind() or "").strip()
            if host_kind:
                actor_id = derive_agent_context_id(
                    host_kind=host_kind,
                    project_root=project_root,
                    host_session_id=hsid,
                )
        except Exception:
            actor_id = ""
    if not actor_id:
        return {}
    # #1007: THE SUBAGENT AXIS. A request that proved a host_agent_id (the CC
    # per-subagent `agent_id`, delivered by the hook stamp or the call claim
    # the daemon took) is a DIFFERENT actor from the conductor whose
    # host_session_id it inherits. Its aidocs_actor_id is the stable
    # agent_context_id derived WITH agent_id -- second-layer, unforgeable from
    # host_session_id (agent_memory_epoch). No host_agent_id ⇒ the conductor's
    # actor, byte for byte as before.
    host_agent_id = _xaacp_current_host_agent_id()
    if host_agent_id:
        try:
            from .agent_memory_epoch import derive_agent_context_id as _derive
            from .agent_memory_epoch import resolve_host_identity as _resolve

            host_kind, _ = _resolve(project_root=project_root)
            child = _derive(
                host_kind=host_kind,
                project_root=project_root,
                host_session_id=hsid,
                agent_id=host_agent_id,
            )
        except Exception:
            child = ""
        if not child:
            return {}
        return {
            "actor_id": child,
            "session_id": session_id,
            "lane_id": "",
            "actor_kind": "subagent",
            "role": "",
            "host_session_id": hsid,
            "host_agent_id": host_agent_id,
        }
    return {
        "actor_id": actor_id,
        "session_id": session_id,
        "lane_id": "",
        "actor_kind": "agent",
        "role": "",
        "host_session_id": hsid,
    }


def _xaacp_current_host_agent_id() -> str:
    """The CC per-subagent id THIS request proved, or "" (no fallback rung)."""
    try:
        from .mcp_server_runtime_helpers import current_calling_agent_id

        return str(current_calling_agent_id() or "").strip()
    except Exception:
        return ""


def xaacp_resolve_caller_route(project_root: Path) -> dict[str, str]:
    """Resolve one canonical actor + session/lane route; fail closed."""
    host_session_id = _xaacp_current_host_session_id()
    if not host_session_id:
        return {}
    # #1007: a subagent shares its parent's host_session_id, which is the key
    # of the worker rows and of msg_role_map. It holds no lane and NO SEAT of
    # its own -- the parent's seat must not be inherited by the accident of a
    # shared key -- so it resolves ONLY through the bound-agent route.
    if _xaacp_current_host_agent_id():
        return _xaacp_bound_agent_route(project_root, host_session_id)
    try:
        from .session_lane_agents_store import SessionLaneAgentsStore

        matches = [
            row
            for row in SessionLaneAgentsStore().get_all_lane_agents(project_root)
            if str(row.get("host_session_id") or "").strip() == host_session_id
        ]
        if len(matches) == 1:
            row = matches[0]
            actor_id = str(row.get("worker_id") or "").strip()
            session_id = str(row.get("session_id") or "").strip()
            lane_id = str(row.get("lane_id") or "").strip()
            if actor_id and session_id and lane_id:
                return {
                    "actor_id": actor_id,
                    "session_id": session_id,
                    "lane_id": lane_id,
                    "actor_kind": "worker",
                    "role": "",
                    "host_session_id": host_session_id,
                    "agent_context_id": str(
                        row.get("agent_context_id") or ""
                    ).strip(),
                }
        if len(matches) > 1:
            return {}
    except Exception:
        logger.exception("XAACP worker route resolution failed")
        return {}
    seat_route = _xaacp_role_route_for_host(project_root, host_session_id)
    if seat_route:
        return seat_route
    # #732: a caller bound to a session but holding no seat is EXACTLY who the
    # seat refusal routes here. Falling through to {} made the named remedy
    # refuse identically to the channel it was substituting for.
    return _xaacp_bound_agent_route(project_root, host_session_id)


def xaacp_resolve_caller_actor(project_root: Path) -> str:
    """Compatibility read of the canonical actor from the full route."""
    route = xaacp_resolve_caller_route(project_root)
    return str(route.get("actor_id") or "").strip() or MSG_ROLE_UNMAPPED


def _xaacp_resolve_target_route(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    lane_id: str,
) -> dict[str, str]:
    """Resolve an exact durable target route; never create phantom mailboxes.

    #640 specimen 3 — TWO defects, one resolver:

      * it required a non-empty ``lane_id`` before it would look ANYTHING up.
        Seats have no lane, so the ``msg_role_map`` branch at the bottom of
        this function was unreachable: there was no ``target_actor_id`` value,
        resolvable or not, that let a caller address the conductor. The lane is
        now required only for the WORKER lookup — the only lookup keyed on it.
      * a lane cannot know the conductor's ``actor_id`` (a sha of project +
        host_kind + host_session_id). It knows the word "conductor". So a
        ``target_actor_id`` naming a SEAT ROLE now resolves through
        ``msg_role_map`` for the caller's OWN session.

    NO NEW PRIVILEGE. Role-name addressing is a TARGET resolver only: the
    caller still sends AS its own canonical actor (``xaacp_dispatch`` passes
    ``sender_actor_id`` from the caller's own route), the seat row must already
    exist durably (a role with no registered seat resolves to ``{}``, so
    phantom mailboxes stay impossible), an ambiguous role resolves to ``{}``,
    and the match is scoped to the caller's bound ``session_id``. A lane may
    REPORT upward; it still cannot post AS a seat — that path is ``msg_send``,
    guarded by ``msg_resolve_caller_role``, and untouched here.
    """
    sid = str(session_id or "").strip()
    target = str(target_actor_id or "").strip()
    lane = str(lane_id or "").strip()
    if not sid or not target:
        return {}

    def _routable(route: dict[str, str]) -> dict[str, str]:
        """Gate a resolved route on the SAME liveness truth the directory shows.

        LOCAL BACKLOG 987 — THE HALF THAT IS NOT PRESENTATION. Grading the
        directory alone would leave the capability fully intact: anyone who had
        RETAINED an old actor_id from a previous generation could still resolve
        a route the directory correctly calls non-addressable, and send into a
        mailbox nobody will ever read. An un-advertised address is not a closed
        one, and a refusal that only stops being suggested is not a refusal.

        Workers are exempt because they are graded by their OWN reported
        lifecycle (`state`) rather than by a binding stamped by whichever server
        generation happened to write it — that is first-hand evidence, and the
        worker branch above already refuses a non-running lane agent.
        """
        if not route or str(route.get("actor_kind") or "") == "worker":
            return route
        hsid = str(route.get("host_session_id") or "").strip()
        liveness = _xaacp_liveness(project_root, session_id=sid)
        if not liveness.get("usable"):
            # Cannot verify => cannot admit (#589). The same fail-closed rule the
            # directory applies, so the two surfaces never disagree even when
            # the oracle itself is down.
            return {}
        verdict = liveness["by_host"].get(hsid)
        # PRESENT AND UNPROVEN is the ghost, and the only thing refused here.
        # ABSENT is not-yet-bound (seat entry precedes managed mode) and is
        # ALLOWED — see `_grade` in `xaacp_directory` for why a ghost can never
        # reach that state. Routing and presentation must agree, so this
        # condition is deliberately the same one, stated once in each place
        # rather than each inventing a rule.
        if verdict is not None and not verdict.get("live"):
            return {}
        return route
    if lane:
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore

            workers = [
                row
                for row in SessionLaneAgentsStore().get_all_lane_agents(project_root)
                if str(row.get("session_id") or "").strip() == sid
                and str(row.get("lane_id") or "").strip() == lane
                and target
                in {
                    str(row.get("worker_id") or "").strip(),
                    str(row.get("agent_context_id") or "").strip(),
                }
            ]
            if len(workers) == 1:
                row = workers[0]
                return {
                    "actor_id": str(row.get("worker_id") or "").strip(),
                    "session_id": sid,
                    "lane_id": lane,
                    "actor_kind": "worker",
                    "host_session_id": str(
                        row.get("host_session_id") or ""
                    ).strip(),
                }
            if len(workers) > 1:
                return {}
        except Exception:
            logger.exception("XAACP target worker route resolution failed")
            return {}

    # Generic bound-agent lookup. Unlike a seat, this grants no role; it merely
    # makes an authenticated session participant addressable by its actor handle.
    # A lane-qualified target can only be a worker, so generic agents require an
    # empty lane route.
    if not lane and target.lower() not in MSG_ROLES:
        try:
            with _connect(project_root) as conn:
                agent_rows = conn.execute(
                    "SELECT actor_id, host_session_id, host_kind, actor_kind, role "
                    "FROM xaacp_actors WHERE actor_id=? AND session_id=? "
                    # #1007: a lane worker's row exists for the directory; it
                    # is ROUTED only through its lane (the worker branch above).
                    "AND actor_kind != 'lane_worker'",
                    (target, sid),
                ).fetchall()
            if len(agent_rows) == 1:
                row = agent_rows[0]
                return _routable(
                    {
                        "actor_id": str(row["actor_id"] or "").strip(),
                        "session_id": sid,
                        "lane_id": "",
                        "actor_kind": "seat" if str(row["role"] or "").strip() else (str(row["actor_kind"] or "agent").strip() or "agent"),
                        "role": str(row["role"] or "").strip(),
                        "host_session_id": str(row["host_session_id"] or "").strip(),
                    }
                )
            if len(agent_rows) > 1:
                return {}
        except Exception:
            logger.exception("XAACP target agent route resolution failed")
            return {}

    # Seat lookup. `target` is either the seat's canonical actor_id (what a
    # XAACP seat authority lives in xaacp_actors. Role names are unique per
    # session there, so cross-surface routing cannot observe two different
    # seat maps. The legacy msg_role_map remains a local compatibility
    # projection only; gate execution never falls back to it.
    is_role_name = target.lower() in MSG_ROLES
    with _connect(project_root) as conn:
        if is_role_name:
            rows = conn.execute(
                "SELECT actor_id, host_session_id, host_kind, actor_kind, role "
                "FROM xaacp_actors WHERE role=? AND session_id=?",
                (target.lower(), sid),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT actor_id, host_session_id, host_kind, actor_kind, role "
                "FROM xaacp_actors WHERE actor_id=? AND session_id=? AND role != ''",
                (target, sid),
            ).fetchall()
    if len(rows) == 1:
        row = rows[0]
        return _routable(
            {
                "actor_id": str(row["actor_id"] or "").strip(),
                "session_id": sid,
                "lane_id": lane,
                "actor_kind": "seat",
                "role": str(row["role"] or "").strip(),
                "host_session_id": str(row["host_session_id"] or "").strip(),
            }
        )
    if len(rows) > 1:
        return {}

    # Unbound/local compatibility only. A gate request is already on the
    # canonical XAACP authority, so consulting msg_role_map there would create
    # the rival-seat authority this cutover removes.
    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        on_gate = bool(current_gate_principal())
    except Exception:
        on_gate = False
    if on_gate:
        return {}
    with _connect(project_root) as conn:
        if is_role_name:
            legacy_rows = conn.execute(
                "SELECT host_session_id, role, actor_id, session_id, host_kind "
                "FROM msg_role_map WHERE role=? AND session_id=? AND actor_id != ''",
                (target.lower(), sid),
            ).fetchall()
        else:
            legacy_rows = conn.execute(
                "SELECT host_session_id, role, actor_id, session_id, host_kind "
                "FROM msg_role_map WHERE actor_id=? AND session_id=?",
                (target, sid),
            ).fetchall()
    if len(legacy_rows) != 1:
        return {}
    row = legacy_rows[0]
    return _routable(
        {
            "actor_id": str(row["actor_id"] or "").strip(),
            "session_id": sid,
            "lane_id": lane,
            "actor_kind": "seat",
            "role": str(row["role"] or "").strip(),
            "host_session_id": str(row["host_session_id"] or "").strip(),
        }
    )


def xaacp_resolve_send_target(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    lane_id: str,
) -> dict:
    """#1061: resolve a SEND target to a durable actor mailbox, or say why not.

    ROLE ADDRESSING IS AN ALIAS, resolved HERE, at send time, in the caller's
    own bound session, through `xaacp_seat_occupancy` -- the single occupancy
    authority. Nothing is stored against a role: the message row names the
    current holder's actor id. No holder refuses (`no_holder`); an occupancy
    read that fails refuses with its own reason.

    A DURABLE ACTOR WHOSE ROUTE IS STALE STILL RECEIVES. A server-generation
    restart may drop the route, never the mailbox, so a known actor of this
    session whose liveness is unproven QUEUES (`route_state` != 'live' ->
    `xaacp_send` reports `delivery='deferred'`). Only an actor this session
    has never known refuses (`unknown_actor`), with the axes it checked.
    `_xaacp_resolve_target_route` keeps its #987 live-only answer for every
    other caller; this function only widens what a SEND may queue to.
    """
    sid = str(session_id or "").strip()
    target = str(target_actor_id or "").strip()
    lane = str(lane_id or "").strip()
    if not sid or not target:
        return {
            "ok": False,
            "status": "invalid",
            "error": "XAACP send requires session_id and target_actor_id",
        }
    if target.lower() in MSG_ROLES:
        seat = target.lower()
        occupancy = xaacp_seat_occupancy(project_root, session_id=sid, role=seat)
        if not occupancy.get("ok"):
            return {
                "ok": False,
                "status": "seat_unresolvable",
                "error": (
                    f"role alias {seat!r} could not be resolved on session {sid!r}: "
                    f"{occupancy.get('error') or occupancy.get('status')}"
                ),
                "session_id": sid,
                "role": seat,
            }
        holder = str(occupancy.get("actor_id") or "").strip()
        if occupancy.get("status") == "vacant" or not holder:
            # ONE-RELEASE COMPAT: an unbound local project may still carry its
            # seat only in the legacy msg_role_map projection. That branch of
            # the live resolver is local-only (never on the gate) and
            # liveness-gated, so it can never store against a dead binding.
            legacy = _xaacp_resolve_target_route(
                project_root, session_id=sid, target_actor_id=seat, lane_id=lane
            )
            if legacy.get("actor_id"):
                return {
                    "ok": True,
                    "route": legacy,
                    "route_state": "live",
                    "resolved_alias": seat,
                    "resolved_via": "legacy_msg_role_map",
                }
            return {
                "ok": False,
                "status": "no_holder",
                "error": (
                    f"no current holder of seat {seat!r} on session {sid!r}; a role "
                    "alias resolves only to the seat's current actor, so nothing was "
                    "queued (use xaacp_directory to address an actor directly)"
                ),
                "session_id": sid,
                "role": seat,
            }
        liveness = str(occupancy.get("liveness") or "")
        return {
            "ok": True,
            "route": {
                "actor_id": holder,
                "session_id": sid,
                "lane_id": lane,
                "actor_kind": "seat",
                "role": seat,
                "host_session_id": str(occupancy.get("host_session_id") or ""),
            },
            "route_state": "live" if liveness == XAACP_SEAT_LIVE else (liveness or "unverified"),
            "resolved_alias": seat,
        }

    live = _xaacp_resolve_target_route(
        project_root, session_id=sid, target_actor_id=target, lane_id=lane
    )
    if live.get("actor_id"):
        return {"ok": True, "route": live, "route_state": "live"}
    if not lane:
        with _connect(project_root) as conn:
            rows = conn.execute(
                "SELECT actor_id, host_session_id, actor_kind, role FROM xaacp_actors "
                "WHERE actor_id=? AND session_id=? AND actor_kind != 'lane_worker'",
                (target, sid),
            ).fetchall()
        if len(rows) == 1:
            row = rows[0]
            role = str(row["role"] or "").strip()
            return {
                "ok": True,
                "route": {
                    "actor_id": str(row["actor_id"]),
                    "session_id": sid,
                    "lane_id": "",
                    "actor_kind": "seat" if role else (str(row["actor_kind"] or "agent") or "agent"),
                    "role": role,
                    "host_session_id": str(row["host_session_id"] or ""),
                },
                "route_state": "unverified",
            }
    return {
        "ok": False,
        "status": "not_found",
        "reason": "unknown_actor",
        "error": (
            f"no durable XAACP actor {target!r} is known on session {sid!r}"
            + (f" lane {lane!r}" if lane else "")
            + " (checked: lane workers, registered actors of this session, seat "
            "role aliases); nothing was queued"
        ),
        "session_id": sid,
        "lane_id": lane,
        "target_actor_id": target,
    }


#: Internal forwarding marker for the stream claim/confirm operations. It rides
#: in `metadata` on a timeout-0 `wait_next` so NO new public ai_msg parameter
#: exists; the reader is always the gate's own derived caller route, so a caller
#: setting it by hand can only claim/confirm ITS OWN events.
_STREAM_OP_KEY = "_stream_op"
XAACP_REMOTE_CLAIM_MAX_ATTEMPTS = 3


def _stream_claim_local(
    project_root: Path,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    reader_actor_kind: str,
    surface: str,
    limit: int,
    lease_seconds: float | None = None,
) -> dict:
    from . import xaacp_stream

    span = _xaacp_spans_all_lanes(reader_actor_kind, lane_id)
    now = time.time()
    notes: list = []
    entries: list[dict] = []
    with _connect(project_root) as conn:
        _xaacp_expire_due(conn, now=now, session_id=session_id)
        floor = 0
        for _ in range(max(1, min(int(limit or 1), XAACP_WAIT_NEXT_BATCH))):
            event = xaacp_stream.claim_next(
                conn,
                session_id=session_id,
                actor_id=actor_id,
                lane_id=lane_id,
                span_all_lanes=span,
                after_cursor=floor,
                via=surface,
                now=now,
                lease_seconds=(
                    xaacp_stream.CLAIM_LEASE_SECONDS if lease_seconds is None else lease_seconds
                ),
                audit=notes,
            )
            if event is None:
                break
            entries.append(_xaacp_stream_entry(event, reader_actor_id=actor_id))
            floor = int(event["seq"])
        conn.commit()
    _stream_transition_audit(project_root, session_id, actor_id, surface, notes)
    return {
        "ok": True,
        "status": "claimed" if entries else "empty",
        "stream_op": "claim",
        "reader_actor_id": actor_id,
        "surface": surface,
        "entries": entries,
        "claim_ids": [e["claim_id"] for e in entries],
    }


def _stream_confirm_local(
    project_root: Path, *, session_id: str, actor_id: str, claim_ids: list[str]
) -> dict:
    from . import xaacp_stream

    with _connect(project_root) as conn:
        confirmed = xaacp_stream.confirm_surfaced(
            conn, session_id=session_id, actor_id=actor_id, claim_ids=list(claim_ids or [])
        )
        conn.commit()
    _stream_transition_audit(
        project_root, session_id, actor_id, "confirm",
        [("surfaced", seq, cid, None) for seq, cid in confirmed],
    )
    return {
        "ok": True,
        "status": "confirmed",
        "stream_op": "confirm",
        "reader_actor_id": actor_id,
        "confirmed_claim_ids": [cid for _, cid in confirmed],
        "unconfirmed_claim_ids": [
            c for c in dict.fromkeys(str(x or "").strip() for x in (claim_ids or []))
            if c and c not in {cid for _, cid in confirmed}
        ],
    }


def _stream_transition_audit(project_root, session_id, actor_id, surface, notes) -> None:
    """stream_deliver, with state claimed | reclaimed | surfaced | abandoned."""
    for state, seq, claim_id, reclaims in notes:
        _audit_event(
            project_root,
            session_id,
            action_kind=f"stream_{state}",
            target_entity=actor_id,
            payload={
                "session_id": session_id,
                "reader_actor_id": actor_id,
                "stream_cursor": seq,
                "claim_id": claim_id,
                "state": state,
                "reclaim_count": reclaims,
                "surface": surface,
            },
            event_kind="stream_deliver",
        )


def _stream_remote_op(
    project_root: Path,
    authority,
    *,
    session_id: str,
    lane_id: str,
    op: dict,
    host_session_id: str = "",
) -> dict:
    """Forward one stream op to the authority; bounded retry, never a silent empty.

    ``host_session_id`` (Stop-hook callers, which hold the host payload but no
    request context) is stamped as this request's host identity so the edge
    carries it in _meta exactly as a tool call would; the gate derives the
    caller route from it.
    """
    last: dict = {}
    for attempt in range(1, XAACP_REMOTE_CLAIM_MAX_ATTEMPTS + 1):
        token = None
        if host_session_id:
            from .mcp_server_runtime_helpers import (
                current_calling_host_kind,
                reset_request_host_identity,
                set_request_host_identity,
            )

            kind = str(current_calling_host_kind() or "").strip()
            token = set_request_host_identity(
                host_session_id, host_kind=kind if kind and kind != "unknown" else "claude_code"
            )
        try:
            last = authority.dispatch(
                project_root,
                mode="wait_next",
                session_id=session_id,
                lane_id=lane_id,
                timeout_seconds=0.0,
                metadata={_STREAM_OP_KEY: op},
            )
        finally:
            if token is not None:
                reset_request_host_identity(token)
        if not isinstance(last, dict):
            last = {"ok": False, "status": "unavailable", "error": "xaacp hub returned a non-object"}
            continue
        transport = last.get("ok") is False and last.get("status") == "unavailable" and (
            "unreachable" in str(last.get("error") or "")
        )
        if not transport:
            break
    else:
        return {**last, "ok": False, "status": "unavailable", "attempts": XAACP_REMOTE_CLAIM_MAX_ATTEMPTS}
    if last.get("ok") is False:
        return last
    if last.get("stream_op") != op.get("op"):
        # An authority that predates the op answered a plain wait_next: do not
        # treat its payload as a claim (it may have replayed or surfaced).
        return {
            "ok": False,
            "status": "unavailable",
            "error": "the XAACP authority does not support stream claim/confirm (older gate build)",
        }
    return last


def xaacp_stream_claim_next(
    project_root: Path,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str = "",
    reader_actor_kind: str = "",
    surface: str,
    limit: int = 1,
) -> dict:
    """THE READ API for a surfacing rail (#1061): PHASE ONE, claim, non-blocking.

    Same cursor and compare-and-set as `xaacp_wait_next`, local or cloud-bound
    alike (a cloud-bound project forwards to the authority; the gate derives the
    reader itself). Returns `{ok, status: claimed|empty, entries, claim_ids}`;
    each entry carries `cursor` and `claim_id`. A claim is IN FLIGHT for
    `CLAIM_LEASE_SECONDS`: call `xaacp_stream_confirm_surfaced` once the notice
    is actually in the returned tool result. Unconfirmed + expired -> claimable
    exactly once more, then abandoned. Transport failure -> `ok: False,
    status: 'unavailable'` after bounded retry, never an empty success.
    """
    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    lane = str(lane_id or "").strip()
    via = str(surface or "").strip()
    if not sid or not aid or not via:
        return {"ok": False, "status": "invalid", "error": "session_id, actor_id and surface are required"}
    if via == "wait_next":
        return {"ok": False, "status": "invalid", "error": "'wait_next' is reserved for the park itself"}
    authority = xaacp_authority_for(project_root)
    if authority is not None:
        if getattr(authority, "reason", ""):
            return {"ok": False, "status": "unavailable", "error": str(authority.reason)}
        out = _stream_remote_op(
            project_root, authority, session_id=sid, lane_id=lane,
            op={"op": "claim", "surface": via, "limit": int(limit or 1)},
        )
        if out.get("ok") and str(out.get("reader_actor_id") or "") != aid:
            return {
                "ok": False,
                "status": "forbidden",
                "error": "the authority derived a different reader than the requesting actor",
                "reader_actor_id": str(out.get("reader_actor_id") or ""),
            }
        return out
    return _stream_claim_local(
        project_root, session_id=sid, actor_id=aid, lane_id=lane,
        reader_actor_kind=reader_actor_kind, surface=via, limit=limit,
    )


# ── consul-stop APIs (#1061, for lane-consul-stop) ───────────────────────

_CONSUL_SEATS = ("conductor", "co_conductor")


def _seat_of_host_local(project_root: Path, *, session_id: str, host_session_id: str) -> dict:
    sid = str(session_id or "").strip()
    hsid = str(host_session_id or "").strip()
    if not sid or not hsid:
        return {"ok": False, "status": "invalid", "error": "session_id and host_session_id required"}
    with _connect(project_root) as conn:
        rows = conn.execute(
            "SELECT actor_id, role FROM xaacp_actors WHERE session_id=? AND host_session_id=? "
            "AND role IN (?, ?) AND host_agent_id=''",
            (sid, hsid, *_CONSUL_SEATS),
        ).fetchall()
    base = {"ok": True, "stream_op": "seat_of_host", "session_id": sid, "host_session_id": hsid}
    if len(rows) != 1:
        return {**base, "seat_role": "", "actor_id": "", "peer_actor_id": None, "peer_live": False,
                **({"ambiguous": True} if rows else {})}
    role = str(rows[0]["role"])
    peer_role = _CONSUL_SEATS[1] if role == _CONSUL_SEATS[0] else _CONSUL_SEATS[0]
    peer = xaacp_seat_occupancy(project_root, session_id=sid, role=peer_role)
    peer_actor = str(peer.get("actor_id") or "") if peer.get("status") == "held" else ""
    return {
        **base,
        "seat_role": role,
        "actor_id": str(rows[0]["actor_id"]),
        "peer_role": peer_role,
        "peer_actor_id": peer_actor or None,
        "peer_live": bool(peer_actor) and peer.get("liveness") == XAACP_SEAT_LIVE,
    }


def _turn_mirror_local(
    project_root: Path,
    *,
    session_id: str,
    host_session_id: str,
    body: str,
    sha256: str,
    pointer: str,
    causal_turn_id: str,
) -> dict:
    from . import xaacp_stream

    turn = str(causal_turn_id or "").strip()
    if not turn or not str(body or "").strip():
        return {"ok": False, "status": "invalid", "error": "body and causal_turn_id required"}
    seat = _seat_of_host_local(project_root, session_id=session_id, host_session_id=host_session_id)
    if not seat.get("ok"):
        return seat
    if not seat["seat_role"]:
        return {"ok": False, "status": "forbidden", "stream_op": "turn_mirror",
                "error": "this host session does not currently hold a Consul seat in the session"}
    sid = seat["session_id"]
    ref = f"turn:{turn}"

    def _existing() -> dict | None:
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT message_id FROM xaacp_stream WHERE session_id=? AND origin='hook' "
                "AND causal_ref=? AND event_kind='incoming' ORDER BY seq LIMIT 1",
                (sid, ref),
            ).fetchone()
        return None if row is None else {
            "ok": True, "stream_op": "turn_mirror", "message_id": str(row[0]), "duplicate": True,
        }

    prior = _existing()
    if prior is not None:
        return prior
    if not seat["peer_actor_id"]:
        return {"ok": False, "status": "peer_absent", "stream_op": "turn_mirror",
                "error": f"no holder of the peer seat {seat.get('peer_role')!r}; nothing mirrored"}
    from .receive_lease_stop import MIRROR_MESSAGE_KIND

    sent = xaacp_send(
        project_root,
        session_id=sid,
        sender_actor_id=seat["actor_id"],
        sender_actor_kind="seat",
        target_actor_id=seat["peer_actor_id"],
        lane_id="",
        message_kind=MIRROR_MESSAGE_KIND,
        body=str(body),
        metadata={"sha256": str(sha256 or ""), "pointer": str(pointer or ""), "causal_turn_id": turn,
                  "seat_role": seat["seat_role"]},
        target_route_state="live" if seat["peer_live"] else "unverified",
        origin=xaacp_stream.ORIGIN_HOOK,
        causal_ref=ref,
    )
    if sent.get("ok") and sent.get("stream_cursor") is None:
        with _connect(project_root) as conn:
            conn.execute("DELETE FROM messages WHERE id=? AND direction='xaacp'", (sent["message_id"],))
            conn.commit()
        return _existing() or {"ok": False, "status": "error", "error": "turn mirror lost a race"}
    if not sent.get("ok"):
        return sent
    return {"ok": True, "stream_op": "turn_mirror", "message_id": sent["message_id"], "duplicate": False,
            "delivery": sent.get("delivery")}


def _last_park_local(project_root: Path, *, session_id: str, actor_id: str) -> dict:
    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    if not sid or not aid:
        return {"ok": False, "status": "invalid", "error": "session_id and actor_id required"}
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT last_park_enter_at, last_park_return_at, last_return_kind FROM xaacp_park_log "
            "WHERE session_id=? AND actor_id=?",
            (sid, aid),
        ).fetchone()
    marker = xaacp_stream_activity_marker(project_root, session_id=sid, actor_id=aid)
    return {
        "ok": True,
        "stream_op": "last_park",
        "session_id": sid,
        "actor_id": aid,
        "last_park_enter_at": row[0] if row else None,
        "last_park_return_at": row[1] if row else None,
        "last_return_kind": str(row[2] or "") if row else "",
        "activity_marker": marker.get("marker", ""),
    }


def _consul_remote_or_local(project_root: Path, *, session_id: str, op: dict, host_session_id: str, local):
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return local()
    if getattr(authority, "reason", ""):
        return {"ok": False, "status": "unavailable", "error": str(authority.reason)}
    return _stream_remote_op(
        project_root, authority, session_id=session_id, lane_id="", op=op, host_session_id=host_session_id
    )


def xaacp_seat_of_host(project_root: Path, *, session_id: str, host_session_id: str) -> dict:
    """Which Consul seat (if any) this host session holds, and its peer. Local or forwarded."""
    return _consul_remote_or_local(
        project_root, session_id=session_id, op={"op": "seat_of_host"}, host_session_id=host_session_id,
        local=lambda: _seat_of_host_local(project_root, session_id=session_id, host_session_id=host_session_id),
    )


def xaacp_turn_mirror_append(
    project_root: Path,
    *,
    session_id: str,
    host_session_id: str,
    body: str,
    sha256: str,
    pointer: str,
    causal_turn_id: str,
) -> dict:
    """Mirror a seated Consul's turn to its peer seat (origin='hook').

    The authority verifies the host session CURRENTLY holds a seat in the
    session (the gate uses its own derived caller host identity, never the
    payload), sends to the peer seat, idempotent on (session, causal_turn_id).
    """
    op = {"op": "turn_mirror", "body": str(body or ""), "sha256": str(sha256 or ""),
          "pointer": str(pointer or ""), "causal_turn_id": str(causal_turn_id or "")}
    return _consul_remote_or_local(
        project_root, session_id=session_id, op=op, host_session_id=host_session_id,
        local=lambda: _turn_mirror_local(
            project_root, session_id=session_id, host_session_id=host_session_id, body=body,
            sha256=sha256, pointer=pointer, causal_turn_id=causal_turn_id,
        ),
    )


def xaacp_last_park(
    project_root: Path, *, session_id: str, actor_id: str, host_session_id: str = ""
) -> dict:
    """The authority's park bookkeeping for one actor + its activity marker.

    From a Stop-hook process on a cloud-bound project pass ``host_session_id``:
    the edge stamps it as the request host identity and the gate derives the
    actor from it; a derived actor different from ``actor_id`` is refused.
    ``activity_marker`` advances ONLY on stream events (messages to/from the
    actor and outcomes) -- not on tool calls.
    """
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return _last_park_local(project_root, session_id=session_id, actor_id=actor_id)
    out = _consul_remote_or_local(
        project_root, session_id=session_id, op={"op": "last_park"}, host_session_id=host_session_id,
        local=lambda: {},
    )
    if out.get("ok") and str(out.get("actor_id") or "") != str(actor_id or "").strip():
        return {"ok": False, "status": "forbidden",
                "error": "the authority derived a different actor than the requested one"}
    return out


OPERATOR_TRANSPORTS = frozenset({"dashboard", "ups", "xaacp"})


def xaacp_session_operator(project_root: Path, *, session_id: str) -> dict:
    """WHO IS THIS SESSION'S OPERATOR (r0). Read-only; fails closed.

    The durable record is the approved host-operator binding
    (host_operator_binding_store). Candidates: the operator bound to the host
    session of each seat holder of this session, plus every approved binding
    whose aidocs_session_id is this session. Exactly ONE distinct user must
    result; none -> unresolvable, several -> ambiguous. Both refuse.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "status": "invalid", "error": "session_id required"}
    try:
        from .host_operator_binding_store import HostOperatorBindingStore

        store = HostOperatorBindingStore()
        users: dict[str, str] = {}
        with _connect(project_root) as conn:
            seats = conn.execute(
                "SELECT role, host_session_id FROM xaacp_actors WHERE session_id=? AND role != ''",
                (sid,),
            ).fetchall()
        for seat in seats:
            uid = store.resolve_operator(project_root, str(seat["host_session_id"] or ""))
            if uid:
                users.setdefault(uid, f"seat:{seat['role']}")
        for binding in store.list_bindings(project_root, statuses=("approved",)):
            if str(binding.aidocs_session_id or "").strip() == sid and binding.operator_user_id:
                users.setdefault(str(binding.operator_user_id), "session_binding")
    except Exception as exc:  # noqa: BLE001 -- unreadable is not a pass
        return {"ok": False, "status": "unavailable", "error": f"operator binding unreadable: {type(exc).__name__}"}
    if not users:
        return {"ok": False, "status": "unresolved", "error": "no approved operator binding for this session"}
    if len(users) > 1:
        return {"ok": False, "status": "ambiguous", "error": "more than one operator is bound to this session"}
    uid, source = next(iter(users.items()))
    return {"ok": True, "operator_user_id": uid, "source": source}


def _operator_principal_refusal(
    operator_principal: dict,
    *,
    project_root: Path | None = None,
    session_id: str = "",
    allow_local_process: bool = False,
) -> tuple[dict | None, dict]:
    """SERVER-SIDE operator check. Returns (refusal, principal_used).

    On the gate the principal is the GATE-RESOLVED one (never the payload): it
    must be an org admin of the bound project, and a supplied principal that
    names a different user is refused. Off the gate (unbound local project)
    this is an in-process call with no MCP surface; the supplied principal is
    recorded with `principal_source='local_process'`.
    """
    supplied = dict(operator_principal or {})
    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        gate_principal = current_gate_principal()
    except Exception:
        gate_principal = None
    if gate_principal:
        from .outer_gate_project_acl import is_org_admin

        try:
            admin = bool(is_org_admin(gate_principal))
        except Exception:
            admin = False
        if not admin:
            return {
                "ok": False,
                "status": "forbidden",
                "error": "operator append requires an authenticated org-admin operator principal",
            }, {}
        claimed = str(supplied.get("user_id") or "").strip()
        actual = str(gate_principal.get("user_id") or "").strip()
        if claimed and claimed != actual:
            return {
                "ok": False,
                "status": "forbidden",
                "error": "supplied operator principal does not match the authenticated principal",
            }, {}
        # ORG ADMIN IS NECESSARY, NOT SUFFICIENT (r0a send-back): the principal
        # must BE this session's bound operator.
        bound = xaacp_session_operator(project_root, session_id=session_id)
        if not bound.get("ok"):
            return {
                "ok": False,
                "status": "forbidden",
                "error": f"the session's bound operator could not be resolved: {bound.get('error')}",
            }, {}
        if bound["operator_user_id"] != actual:
            return {
                "ok": False,
                "status": "forbidden",
                "error": "the authenticated principal is not this session's bound operator",
            }, {}
        return None, {"user_id": actual, "principal_source": f"gate:{bound['source']}"}
    if not allow_local_process:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "operator append through a tool dispatch requires a gate-resolved principal",
        }, {}
    user = str(supplied.get("user_id") or "").strip()
    if not user:
        return {"ok": False, "status": "forbidden", "error": "operator principal user_id required"}, {}
    return None, {"user_id": user, "principal_source": "local_process"}


def xaacp_operator_append(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    body: str,
    causal_ref: str,
    operator_principal: dict,
    transport: str,
) -> dict:
    """THE OPERATOR BRIDGE APPEND (#1061, for lane-operator-bridge), on the authority.

    In-process API. The off-gate `local_process` principal path is reachable
    ONLY here; the ai_msg dispatch route calls `_xaacp_operator_append` with
    local-process trust disabled, so a tool call can never use it.
    """
    return _xaacp_operator_append(
        project_root,
        session_id=session_id,
        target_actor_id=target_actor_id,
        body=body,
        causal_ref=causal_ref,
        operator_principal=operator_principal,
        transport=transport,
        _allow_local_process=True,
    )


def _xaacp_operator_append(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    body: str,
    causal_ref: str,
    operator_principal: dict,
    transport: str,
    _allow_local_process: bool,
) -> dict:
    """Implementation of :func:`xaacp_operator_append`.

    Stores one `origin='operator'` incoming stream entry (xaacp_send) behind the
    server-side principal check. IDEMPOTENT on (session_id, origin='operator',
    causal_ref): a repeat returns the ORIGINALLY stored message and target and
    never re-resolves the target. Role aliases resolve once, at first append.
    Sender is `operator:<user_id>` from the verified principal. Nothing here is
    reachable as an ai_msg parameter: origin/causal_ref have no public spelling.
    """
    from . import xaacp_stream

    sid = str(session_id or "").strip()
    ref = str(causal_ref or "").strip()
    via = str(transport or "").strip().lower()
    if not sid or not ref or not str(target_actor_id or "").strip() or not str(body or "").strip():
        return {"ok": False, "status": "invalid", "error": "session_id, target_actor_id, body and causal_ref are required"}
    if via not in OPERATOR_TRANSPORTS:
        return {"ok": False, "status": "invalid", "error": f"transport must be one of {sorted(OPERATOR_TRANSPORTS)}"}
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return refused
    denied, principal = _operator_principal_refusal(
        operator_principal,
        project_root=project_root,
        session_id=sid,
        allow_local_process=_allow_local_process,
    )
    if denied is not None:
        return denied

    def _existing() -> dict | None:
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT s.seq, s.message_id, s.reader_actor_id FROM xaacp_stream s "
                "WHERE s.session_id=? AND s.origin='operator' AND s.causal_ref=? "
                "AND s.event_kind='incoming' ORDER BY s.seq LIMIT 1",
                (sid, ref),
            ).fetchone()
        if row is None:
            return None
        return {
            "ok": True,
            "status": "pending",
            "idempotent_replay": True,
            "message_id": str(row["message_id"]),
            "target_actor_id": str(row["reader_actor_id"]),
            "stream_cursor": int(row["seq"]),
            "origin": xaacp_stream.ORIGIN_OPERATOR,
            "causal_ref": ref,
        }

    prior = _existing()
    if prior is not None:
        return prior
    resolution = xaacp_resolve_send_target(
        project_root, session_id=sid, target_actor_id=target_actor_id, lane_id=""
    )
    if not resolution.get("ok"):
        return resolution
    target = str(resolution["route"]["actor_id"])
    sent = xaacp_send(
        project_root,
        session_id=sid,
        sender_actor_id=f"operator:{principal['user_id']}",
        sender_actor_kind="seat",
        target_actor_id=target,
        lane_id="",
        message_kind="operator_prompt",
        body=str(body),
        metadata={"operator_transport": via, "principal_source": principal["principal_source"]},
        target_route_state=str(resolution.get("route_state") or "live"),
        origin=xaacp_stream.ORIGIN_OPERATOR,
        causal_ref=ref,
    )
    if sent.get("ok") and sent.get("stream_cursor") is None:
        # Lost the idempotency race to a concurrent append of the same ref: the
        # unique index refused our stream entry. Drop our orphan row; answer the
        # winner's.
        with _connect(project_root) as conn:
            conn.execute("DELETE FROM messages WHERE id=? AND direction='xaacp'", (sent["message_id"],))
            conn.commit()
        return _existing() or {"ok": False, "status": "error", "error": "operator append lost a race and found no winner"}
    if not sent.get("ok"):
        return sent
    return {
        **sent,
        "idempotent_replay": False,
        "target_actor_id": target,
        "origin": xaacp_stream.ORIGIN_OPERATOR,
        "causal_ref": ref,
    }


def xaacp_stream_confirm_surfaced(
    project_root: Path,
    *,
    session_id: str,
    actor_id: str,
    claim_ids: list[str],
) -> dict:
    """PHASE TWO: the claimed entries were rendered. Local or cloud-bound alike.

    Returns `{ok, confirmed_claim_ids, unconfirmed_claim_ids}`. A claim id that
    was re-issued after lease expiry, abandoned, or belongs to another reader
    is unconfirmed. A confirmed event never surfaces again.
    """
    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    ids = [str(c or "").strip() for c in (claim_ids or []) if str(c or "").strip()]
    if not sid or not aid:
        return {"ok": False, "status": "invalid", "error": "session_id and actor_id are required"}
    authority = xaacp_authority_for(project_root)
    if authority is not None:
        if getattr(authority, "reason", ""):
            return {"ok": False, "status": "unavailable", "error": str(authority.reason)}
        out = _stream_remote_op(
            project_root, authority, session_id=sid, lane_id="",
            op={"op": "confirm", "claim_ids": ids},
        )
        if out.get("ok") and str(out.get("reader_actor_id") or "") != aid:
            return {"ok": False, "status": "forbidden", "error": "the authority derived a different reader than the requesting actor"}
        return out
    return _stream_confirm_local(project_root, session_id=sid, actor_id=aid, claim_ids=ids)


def xaacp_register_actor(
    project_root: Path,
    *,
    actor_id: str,
    host_session_id: str,
    host_kind: str,
    session_id: str,
    actor_kind: str = "agent",
    source: str = "xaacp_call",
    host_agent_id: str = "",
    worker_id: str = "",
    lane_id: str = "",
) -> None:
    """Upsert one non-worker actor and stamp positive current-generation presence.

    The boot stamp proves only that this actor spoke through this MCP generation;
    it is never used to infer death when a later generation does not match.

    #1007: ``host_agent_id`` / ``worker_id`` / ``lane_id`` extend the SAME row.
    A generic ``actor_kind='agent'`` re-registration (every XAACP call does one)
    never DOWNGRADES a row already established as conductor/subagent/lane_worker.
    """
    aid = str(actor_id or "").strip()
    hsid = str(host_session_id or "").strip()
    hkind = str(host_kind or "").strip()
    sid = str(session_id or "").strip()
    kind = str(actor_kind or "agent").strip() or "agent"
    if not aid or not hsid or not sid:
        return
    # Positive presence, never a death predicate. Every real XAACP call reaches
    # this writer through xaacp_dispatch, so the process boot token proves only
    # that THIS actor has spoken through THIS MCP generation. A later generation
    # mismatch means "not observed here", never "dead"; agent_audit keeps that
    # distinction and can still prove the actor via another positive rung.
    try:
        from .managed_mode_service import current_boot_token

        seen_boot = str(current_boot_token() or "").strip()
    except Exception:  # noqa: BLE001 — absent evidence is not registration failure
        seen_boot = ""
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT INTO xaacp_actors "
            "(actor_id, host_session_id, host_kind, session_id, actor_kind, source, "
            "last_seen_boot_token, updated_at, host_agent_id, worker_id, lane_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET "
            "host_session_id=excluded.host_session_id, host_kind=excluded.host_kind, "
            "session_id=excluded.session_id, "
            "actor_kind=CASE WHEN excluded.actor_kind='agent' "
            "AND xaacp_actors.actor_kind IN ('conductor','subagent','lane_worker') "
            "THEN xaacp_actors.actor_kind ELSE excluded.actor_kind END, "
            "source=excluded.source, last_seen_boot_token=excluded.last_seen_boot_token, "
            "updated_at=excluded.updated_at, "
            "host_agent_id=CASE WHEN excluded.host_agent_id='' "
            "THEN xaacp_actors.host_agent_id ELSE excluded.host_agent_id END, "
            "worker_id=CASE WHEN excluded.worker_id='' "
            "THEN xaacp_actors.worker_id ELSE excluded.worker_id END, "
            "lane_id=CASE WHEN excluded.lane_id='' "
            "THEN xaacp_actors.lane_id ELSE excluded.lane_id END",
            (
                aid,
                hsid,
                hkind,
                sid,
                kind,
                str(source or ""),
                seen_boot,
                time.time(),
                str(host_agent_id or "").strip(),
                str(worker_id or "").strip(),
                str(lane_id or "").strip(),
            ),
        )
        conn.commit()


def _xaacp_session_for_host(project_root: Path, host_session_id: str) -> str:
    """The AIDOCS session a host session is bound to, or "" (no substitute)."""
    try:
        from .managed_mode_service import ManagedModeService, resolve_managed_session

        return resolve_managed_session(
            ManagedModeService(), project_root, host_session_id=host_session_id
        )
    except Exception:
        logger.exception("XAACP host->session resolution failed")
    return ""


def xaacp_register_host_actor(
    project_root: Path,
    *,
    host_session_id: str,
    host_kind: str,
    actor_kind: str,
    host_agent_id: str = "",
    source: str,
) -> str:
    """Establish the actor row for a HOST-announced identity (#1007).

    Called at SessionStart (conductor) and SubagentStart (subagent). The
    aidocs_actor_id is the stable agent_context_id -- derived WITH the
    host_agent_id for a subagent, so it never collides with the conductor that
    shares its host_session_id. Returns the actor_id, or "" when nothing could
    be established (no host session, no managed binding, no kind): no row is
    ever minted from a guess.
    """
    hsid = str(host_session_id or "").strip()
    hkind = str(host_kind or "").strip()
    kind = str(actor_kind or "").strip()
    agent = str(host_agent_id or "").strip()
    if not hsid or not hkind or kind not in {"conductor", "subagent"}:
        return ""
    if kind == "subagent" and not agent:
        # A blank agent_id is the MAIN thread; registering it as a subagent
        # would shadow the conductor's own row.
        return ""
    session_id = _xaacp_session_for_host(project_root, hsid)
    if not session_id:
        return ""
    from .agent_memory_epoch import derive_agent_context_id

    actor_id = derive_agent_context_id(
        host_kind=hkind,
        project_root=project_root,
        host_session_id=hsid,
        agent_id=agent or None,
    )
    if not actor_id:
        return ""
    xaacp_register_actor(
        project_root,
        actor_id=actor_id,
        host_session_id=hsid,
        host_kind=hkind,
        session_id=session_id,
        actor_kind=kind,
        source=source,
        host_agent_id=agent,
    )
    return actor_id


# ── #1007 transport channel: one-shot call claims ─────────────────────────
#
# WHY NOT A HEADER. Claude Code adds no agent_id to the MCP request, and the
# stdio shim is ONE process per window shared by every subagent of that window
# (measured 2026-09-03: parent and subagent collapse to one actor). The shim
# cannot know which agent is speaking, so a header set from the hook stamp is
# impossible without a side channel -- which is what this is, made explicit.
#
# THE CORRELATION. The in-subagent PreToolUse hook fires BEFORE the MCP
# request is sent and holds `agent_id`, `tool_name` and `tool_input` -- the
# exact (name, arguments) the daemon receives moments later. The hook records a
# claim keyed on (host_session_id, tool_name, sha256(canonical arguments)); the
# daemon takes the OLDEST matching claim on arrival. Concurrent siblings issuing
# byte-identical calls are the one ambiguity: two DIFFERENT agents holding the
# same key are REFUSED (#1015, below), never guessed and never demoted to the
# conductor.
_XAACP_CALL_CLAIM_TTL_SECONDS = 600.0
# ── #1015: FAIL CLOSED, and the MAIN-THREAD MARKER that makes it possible ──
#
# OPERATOR RULING 2026-09-04 (backlog #1015): "make ambiguous/missing subagent
# attribution fail closed instead of inheriting the parent". The comment above
# describes the fallback that ruling RETIRES: an unattributable subagent call
# must NOT quietly become the conductor's.
#
# The obstacle is that "the main thread called" and "a subagent called and its
# claim is gone" both arrive as a bare ABSENCE. So the hook now claims EVERY
# mcp__aidocs__* call and writes this marker when the payload carries no
# agent_id. Absence and "absence of a subagent" stop being the same fact.
#
# The marker is deliberately NOT a plausible host agent id (CC's are bare hex),
# so no host value can ever forge it.
XAACP_MAIN_THREAD_AGENT = "__main_thread__"


def xaacp_claim_channel_is_live(project_root: Path, *, host_session_id: str) -> bool:
    """Has this host session's PreToolUse hook EVER reached this daemon?

    The discriminator between "a host that never claims" (lane workers, the
    Outer Gate, any non-CC surface -- unchanged, they never had a hook) and "a
    governed conversation whose gate did not see this call" (fail closed).
    """
    hsid = str(host_session_id or "").strip()
    if not hsid:
        return False
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT 1 FROM xaacp_claim_channel WHERE host_session_id=?",
            (hsid,),
        ).fetchone()
    return row is not None


def xaacp_attribution_refusal_message(verdict: dict) -> str:
    """Render a refusal verdict as the honest one-line error the caller sees."""
    error = str(verdict.get("error") or "subagent_attribution_missing")
    tool = str(verdict.get("tool_name") or "?")
    matched = verdict.get("matched")
    if error == "subagent_attribution_ambiguous":
        why = (
            f"{matched} live call claims from DIFFERENT agents match this exact "
            "call, so which one issued it cannot be known"
        )
    else:
        why = (
            f"{matched} live call claims match this call, and this conversation's "
            "PreToolUse gate has claimed before -- so this call was never seen by it "
            "(claim absent or expired)"
        )
    return (
        f"forbidden: {error} -- {tool}: {why}. Refusing rather than attributing it "
        "to the conductor (operator ruling 2026-09-04, backlog #1015). Remedy: "
        "retry the call (a fresh PreToolUse claim is written per call); if it keeps "
        "failing, the hook broker is down -- check GATE HEALTH."
    )


def xaacp_call_claim_key(tool_name: str, arguments) -> tuple[str, str]:
    """``(tool_name, args_hash)`` -- ONE canonicalisation at both ends of the wire.

    ONE NAME AT BOTH ENDS, and this is where that promise is kept. The
    in-subagent PreToolUse hook is handed the HOST's tool name
    (``mcp__aidocs__ai_msg``); the daemon is handed the tool as REGISTERED
    (``ai_msg``). Keyed verbatim the two NEVER matched, so on a conversation
    whose claim channel was live every call resolved to zero claims and was
    refused -- measured 2026-09-04 on build 249, where it locked the conductor
    out of every managed tool within seconds of the runtime swap. The host's
    ``mcp__<server>__`` prefix is therefore stripped here, at the one place
    both ends already share, rather than at either caller.
    """
    import hashlib

    name = str(tool_name or "").strip()
    if name.startswith("mcp__"):
        # "mcp__aidocs__ai_msg" -> "ai_msg". A name carrying no prefix is left
        # exactly as it is, so a non-MCP caller keys on what it passed.
        _prefix, _sep, _bare = name.rpartition("__")
        name = _bare or name
    try:
        canonical = json.dumps(
            arguments if isinstance(arguments, dict) else {},
            sort_keys=True,
            separators=(",", ":"),
            # #1017 HARDENING, not the cure. The cure is that both ends now
            # decode the payload as UTF-8 (read_hook_payload_text). This makes
            # the canonical form pure ASCII as well, so the key cannot be
            # altered by any codec that mangles non-ASCII on either side --
            # belt and braces on a value whose whole job is to be identical in
            # two processes.
            ensure_ascii=True,
            default=str,
        )
    except Exception:
        canonical = "{}"
    return name, hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def xaacp_record_call_claim(
    project_root: Path,
    *,
    host_session_id: str,
    host_agent_id: str,
    tool_name: str,
    tool_input,
) -> bool:
    """Record that ``host_agent_id`` is about to issue this MCP call."""
    hsid = str(host_session_id or "").strip()
    # #1015: a BLANK agent is the MAIN THREAD, and it is claimed too. Recording
    # nothing for it would leave the daemon unable to tell it from a subagent
    # whose claim went missing -- the whole point of the marker.
    agent = str(host_agent_id or "").strip() or XAACP_MAIN_THREAD_AGENT
    name, args_hash = xaacp_call_claim_key(tool_name, tool_input)
    if not hsid or not name:
        return False
    now = time.time()
    with _connect(project_root) as conn:
        conn.execute(
            "DELETE FROM xaacp_call_claims WHERE created_at < ?",
            (now - _XAACP_CALL_CLAIM_TTL_SECONDS,),
        )
        conn.execute(
            "INSERT INTO xaacp_call_claims "
            "(claim_id, host_session_id, tool_name, args_hash, host_agent_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (uuid4().hex, hsid, name, args_hash, agent, now),
        )
        # The watermark: this conversation's gate demonstrably reaches us. It
        # outlives the claim rows on purpose (see the table comment).
        conn.execute(
            "INSERT INTO xaacp_claim_channel (host_session_id, first_seen_at, last_seen_at) "
            "VALUES (?,?,?) "
            "ON CONFLICT(host_session_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (hsid, now, now),
        )
        conn.commit()
    return True


def xaacp_resolve_call_attribution(
    project_root: Path,
    *,
    host_session_id: str,
    tool_name: str,
    arguments,
) -> dict:
    """#1015: WHO issued this arriving MCP call -- or a refusal saying why not.

    Four outcomes, and only the first two ever produce an identity:

      ``attributed``      one live claim key, one agent -> that subagent.
      ``main_thread``     the claim carries the MAIN-THREAD MARKER -> the
                          conductor's own actor, byte for byte as before.
      ``unclaimed_host``  this host session has NEVER claimed, so it has no
                          PreToolUse hook at all (lane workers, the Outer
                          Gate, non-CC hosts) -> unchanged behaviour.
      ``forbidden``       a governed conversation whose gate HAS claimed
                          before, but this call has no live claim
                          (``subagent_attribution_missing``) or has claims
                          from two different agents
                          (``subagent_attribution_ambiguous``). NEVER the
                          conductor -- guessing here is exactly the defect.
    """
    hsid = str(host_session_id or "").strip()
    name, args_hash = xaacp_call_claim_key(tool_name, arguments)
    if not hsid or not name:
        # No identity axis at all: there is nothing to attribute and nothing to
        # steal. This is #672's honest empty, not a refusal.
        return {"ok": True, "status": "unclaimed_host", "host_agent_id": "", "matched": 0}
    now = time.time()
    with _connect(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT claim_id, host_agent_id FROM xaacp_call_claims "
            "WHERE host_session_id=? AND tool_name=? AND args_hash=? AND created_at>=? "
            "ORDER BY created_at ASC",
            (hsid, name, args_hash, now - _XAACP_CALL_CLAIM_TTL_SECONDS),
        ).fetchall()
        agents = {str(r["host_agent_id"] or "").strip() for r in rows}
        # A claim is CONSUMED only when it attributes. A refusal (and the
        # no-claim case) leaves the table alone, so the sibling calls that
        # follow still have theirs.
        if rows and len(agents) == 1:
            conn.execute(
                "DELETE FROM xaacp_call_claims WHERE claim_id=?",
                (str(rows[0]["claim_id"]),),
            )
        conn.commit()

    if not rows:
        # The write lock is released before this: the watermark answers on its
        # own connection, and holding BEGIN IMMEDIATE across it would deadlock
        # this reader against itself.
        if not xaacp_claim_channel_is_live(project_root, host_session_id=hsid):
            return {"ok": True, "status": "unclaimed_host", "host_agent_id": "", "matched": 0}
        return {
            "ok": False,
            "status": "forbidden",
            "error": "subagent_attribution_missing",
            "tool_name": name,
            "host_agent_id": "",
            "matched": 0,
        }
    if len(agents) != 1:
        # Two different agents, one byte-identical call. The marker takes part:
        # "it MIGHT be the main thread" is not licence to hand over the
        # conductor's actor.
        return {
            "ok": False,
            "status": "forbidden",
            "error": "subagent_attribution_ambiguous",
            "tool_name": name,
            "host_agent_id": "",
            "matched": len(rows),
        }
    agent = agents.pop()
    if agent == XAACP_MAIN_THREAD_AGENT:
        return {"ok": True, "status": "main_thread", "host_agent_id": "", "matched": len(rows)}
    return {"ok": True, "status": "attributed", "host_agent_id": agent, "matched": len(rows)}


# NOTE (#1015): `xaacp_take_call_claim` lived here as a thin projection of
# `xaacp_resolve_call_attribution` down to "the subagent id, or empty". It was
# REMOVED rather than allowlisted: nothing in production called it, and its own
# docstring conceded it was not an authority — "" folded main_thread,
# unclaimed_host and BOTH refusals into one value, which is precisely the
# absence-vs-negative collapse the fail-closed ruling exists to end. A
# projection with no consumer is not a capability (law 183074ae). The tests
# that wanted the terse shape now build it locally, where it can mislead
# nobody. Use `xaacp_resolve_call_attribution` and read its `status`.


def xaacp_claim_seat(
    project_root: Path,
    *,
    actor_id: str,
    session_id: str,
    role: str,
) -> dict:
    """Claim one XAACP seat on the authoritative actor registry.

    One role, one actor, one session. This is XAACP authority only; legacy
    ``msg_role_map`` may remain as a local compatibility projection, but role
    routing/discovery for XAACP comes from this unique registry.
    """
    aid = str(actor_id or "").strip()
    sid = str(session_id or "").strip()
    seat = str(role or "").strip().lower()
    if not aid or not sid:
        return {"ok": False, "status": "invalid", "error": "actor_id and session_id required"}
    if seat not in {"conductor", "co_conductor"}:
        return {
            "ok": False,
            "status": "invalid",
            "error": "XAACP seat claim is restricted to conductor/co_conductor; king has no ai_seat entry mode",
        }
    # ONE OCCUPANCY AUTHORITY, READ BEFORE THE WRITE LOCK. The claim used to ask
    # its own question here -- "is there a row" -- while the succession asked
    # "is there a row AND is its holder alive or stale". Two definitions of
    # `occupied`, and on the live box they answered OPPOSITELY for the same
    # seat, which sent the caller round a circle: co-enter said occupied,
    # succeed said vacant-claim-it-instead, co-enter said occupied. Both doors
    # now read `xaacp_seat_occupancy` and cannot disagree. It opens its own
    # connection, so it is called BEFORE `BEGIN IMMEDIATE`, never inside it.
    occupancy = xaacp_seat_occupancy(project_root, session_id=sid, role=seat, actor_id=aid)
    if occupancy.get("status") == "held":
        return {
            "ok": False,
            "status": "occupied",
            "error": occupancy["error"],
            "role": seat,
            "incumbent_actor_id": occupancy.get("actor_id", ""),
            "incumbent_liveness": occupancy.get("liveness", ""),
            "incumbent_idle_seconds": occupancy.get("idle_seconds", 0.0),
            "grace_seconds": occupancy.get("grace_seconds", 0.0),
            "next_call": occupancy.get("next_call", ""),
        }
    with _connect(project_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT actor_id FROM xaacp_actors WHERE session_id=? AND role=?",
            (sid, seat),
        ).fetchone()
        if row is not None and str(row["actor_id"] or "").strip() != aid:
            # RACE GUARD ONLY. The graded verdict above already passed; someone
            # took the seat between the grade and the lock.
            conn.rollback()
            return {
                "ok": False,
                "status": "occupied",
                "error": (
                    f"XAACP seat {seat!r} on session {sid!r} was taken while this "
                    "claim was being graded; re-read it with ai_seat(mode='status')"
                ),
                "role": seat,
                "incumbent_actor_id": str(row["actor_id"] or "").strip(),
            }
        own = conn.execute(
            "SELECT actor_id FROM xaacp_actors WHERE actor_id=? AND session_id=?",
            (aid, sid),
        ).fetchone()
        if own is None:
            return {"ok": False, "status": "forbidden", "error": "actor must be registered before claiming a seat"}
        conn.execute(
            "UPDATE xaacp_actors SET role='' WHERE actor_id=? AND session_id=?",
            (aid, sid),
        )
        conn.execute(
            "UPDATE xaacp_actors SET role=?, updated_at=? WHERE actor_id=? AND session_id=?",
            (seat, time.time(), aid, sid),
        )
        conn.commit()
    return {"ok": True, "status": "claimed", "actor_id": aid, "session_id": sid, "role": seat}


def xaacp_claim_current_seat(
    project_root: Path,
    *,
    session_id: str,
    role: str,
) -> dict:
    """Claim a seat for the authenticated/current actor without inventing identity.

    Seat entry may be the operation that establishes local managed-mode state, so
    it cannot require an already-seated/managed XAACP route. The actor identity
    itself must still be provable from the current request context.
    """
    from .mcp_server_runtime_helpers import (
        current_calling_agent_context_id,
        current_calling_host_kind,
        current_calling_host_session_id,
    )

    hsid = str(current_calling_host_session_id() or "").strip()
    aid = str(current_calling_agent_context_id(project_root) or "").strip()
    sid = str(session_id or "").strip()
    if not hsid or not aid or not sid:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "seat claim requires a provable current actor and explicit session_id",
        }
    # A SPAWNED AGENT NEVER HOLDS A SEAT, so it never TAKES one either. The
    # succession door has enforced this since DRT-10; the entry door did not,
    # which left the rule true only of the harder path. Same rule, one spelling.
    spawned = _xaacp_spawned_agent_refusal(
        xaacp_resolve_caller_route(project_root), role=role, verb="claim"
    )
    if spawned is not None:
        return spawned
    xaacp_register_actor(
        project_root,
        actor_id=aid,
        host_session_id=hsid,
        host_kind=str(current_calling_host_kind() or "").strip(),
        session_id=sid,
        actor_kind="agent",
        source="ai_seat",
    )
    return xaacp_claim_seat(project_root, actor_id=aid, session_id=sid, role=role)


def xaacp_claim_seat_authority(
    project_root: Path,
    *,
    session_id: str,
    role: str,
    confirm_token: str = "",
) -> dict:
    """Claim through the ONE XAACP authority; local SQLite only when unbound."""
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return xaacp_claim_current_seat(project_root, session_id=session_id, role=role)
    claim = getattr(authority, "claim_seat", None)
    if not callable(claim):
        return {
            "ok": False,
            "status": "unavailable",
            "error": "XAACP authority does not implement seat claims",
        }
    return claim(
        Path(project_root),
        session_id=str(session_id or ""),
        role=str(role or ""),
        confirm_token=str(confirm_token or ""),
    )


# ---------------------------------------------------------------------------
# SEAT SUCCESSION (DRT-10)
# ---------------------------------------------------------------------------
# THE DEFECT. `xaacp_claim_seat` allows exactly one actor per (session, role)
# and that is CORRECT -- one conductor and one co-conductor per session is the
# operator's law, and `test_xaacp_seat_authority_allows_one_actor_per_session_
# role` is its contract. What the registry had no word for was SUCCESSION. A
# Web-ChatGPT co-conductor conversation that dies (context exhausted, deleted,
# closed) leaves its actor holding `co_conductor` forever: the only
# `UPDATE xaacp_actors SET role=''` in the tree is an actor clearing its OWN
# role before taking another, which a dead conversation can never run, and the
# replacement conversation is a legitimately DIFFERENT actor, so it is refused
# `occupied` with no remedy. The seat was bricked.
#
# SUCCESSION IS NOT EVICTION. A live actor is never displaced by someone merely
# claiming the incumbent is dead -- that claim is the hostile takeover this
# primitive has to be distinguishable from. So the transfer demands one of two
# things the claimant cannot manufacture:
#
#   * LEASE STALENESS. The incumbent's own heartbeat (`xaacp_actors.updated_at`,
#     restamped by every XAACP call through `xaacp_register_actor`) is older
#     than a grace period the claimant may only LENGTHEN, and the liveness
#     audit does not prove the incumbent live. This is the web-conversation
#     case: there is no pid and no window to probe, so the heartbeat is the only
#     evidence that exists.
#   * AN OPERATOR-ORDERED HANDOVER, recorded as a standing `approval_authority` on
#     this seat -- request by one actor, decision by a DIFFERENT one, both
#     identities derived the way `ai_msg` derives its sender. Reused, never
#     re-invented: a second approval mechanism beside that one is the
#     rival-definition breach this project keeps paying for. But two parties are
#     not enough when they are PEERS, so this route additionally demands that the
#     approver rank r0 or above -- see `_xaacp_seat_handover_authority`, which
#     fails closed today because the rank ladder is not landed.
#
# WHO MAY HOLD A SEAT AT ALL. A conductor and a co-conductor are SEATS; a lane
# and a subagent are the SAME THING as each other -- a spawned agent, ranked
# r0.x.y.z -- and how the host spawned it (`claude -p -r`, `opencode -q -s`, a
# host-internal spawn) is an adapter detail that carries no authority and must
# never reach an authority decision. A spawned agent never holds a seat, so it
# never succeeds to one either; that refusal is enforced below.
#
# A POSITIVE LIVENESS PROOF IS A VETO AND NEVER A GRANT. `_xaacp_liveness` is
# borrowed whole, exactly as `xaacp_directory` borrows it, and only its positive
# rung is read. Absence from the projection is UNKNOWN -- it neither proves the
# incumbent dead (staleness must still be shown) nor protects it. That is
# deliberate: `_grade` treats an absent binding as ASSUMED live for
# addressability, and importing that assumption here would make every web actor
# un-succeedable, since a ChatGPT conversation never holds a local binding.
#
# The AIDOCS work-session id is the CONTINUITY TARGET being inherited, never
# the actor identity. If it were the identity, every successor would resolve to
# the incumbent and this whole surface would be a no-op that looked like a fix.

# The `approval_authority` subject kind for a seat succession. That primitive is
# general by design -- `(subject_kind, subject_id)` -- so this needs no schema
# change there and no edit to it.
XAACP_SEAT_SUCCESSION_SUBJECT_KIND = "xaacp_seat"

# HOW LONG SILENCE MUST LAST BEFORE IT IS EVIDENCE. Long enough that a live
# conversation idling between operator turns is never mistaken for a corpse,
# short enough that a stranded seat is recoverable the same working day. It is a
# FLOOR, not a default: `grace_seconds` may raise it and can never lower it.
XAACP_SEAT_STALE_GRACE_SECONDS = 6 * 3600

# Where the legacy projection's row says the seat came from, so a reader years
# later can tell an inherited seat from a claimed one.
MSG_SEAT_SOURCE_SUCCESSION = "xaacp_succession"


def xaacp_seat_succession_subject(*, session_id: str, role: str) -> str:
    """The ONE `approval_authority` subject_id for a seat. One spelling, one home."""
    return f"{str(session_id or '').strip()}:{str(role or '').strip().lower()}"


def _xaacp_seat_handover_authority(
    project_root: Path,
    *,
    session_id: str,
    role: str,
    subject: str,
) -> dict:
    """May a PROVABLY LIVE seat be transferred right now, and on whose word?

    TWO PARTIES ARE NOT ENOUGH WHEN THEY ARE PEERS. `r0a` (conductor) and `r0b`
    (co-conductor) hold equal authority under `r0`, so a rule that accepted "two
    actors agreed" would let the co-conductor plus any other live seat-holder
    lift a working conductor out of its seat and leave a clean audit trail
    calling it authorized. That is a coup with paperwork. The Empire's authority
    ladder (ruled 2026-09-10) is explicit: authority never rises going down, and
    a grantor must be a STRICT ANCESTOR of the grantee -- never self, never a
    peer. Displacing a live seat is an authority transfer, so only `r0` or above
    may order one.

    `approval_authority` stays the RECORD of that order -- one mechanism, not two --
    but a standing verdict is now a necessary and insufficient condition: the
    approver's RANK must also be proven to be r0 or above.

    FAILS CLOSED, AND SAYS WHY. The rank ladder that could prove an approver is
    a strict ancestor is not landed yet, so no approver can be verified and this
    returns a refusal naming the missing check. Case 1 is therefore deliberately
    unreachable today. The alternative -- accepting a peer's approval until the
    rank model arrives -- would ship exactly the coup vector the ruling exists to
    close, and would do it in the one code path whose audit trail reads
    "authorized".
    """
    try:
        from . import approval_authority

        standing = bool(
            approval_authority.is_approved(
                project_root, XAACP_SEAT_SUCCESSION_SUBJECT_KIND, subject
            )
        )
    except Exception:  # noqa: BLE001 -- an unreadable approval is NOT an approval
        logger.exception("seat handover approval read failed; treating as UNAPPROVED")
        standing = False

    # THE RANK CHECK THAT DOES NOT EXIST YET. There is deliberately no fallback
    # here: "we could not check the rank" is not "the rank was fine".
    return {
        "ok": False,
        "status": "operator_authority_required",
        "approval_standing": standing,
        "error": (
            f"seat {role!r} on session {session_id!r} is held by a PROVABLY LIVE "
            "actor, so transferring it is an authority transfer that only r0 or "
            "above may order — a peer approval can never authorize it. "
            + (
                "An approval stands on "
                f"{XAACP_SEAT_SUCCESSION_SUBJECT_KIND}:{subject}, but the approver's "
                "rank cannot be verified"
                if standing
                else "No approval stands, and approver rank cannot be verified"
            )
            + ": the authority-rank ladder that proves an approver is a strict "
            "ancestor is not available, so NO handover of a live seat can be "
            "authorized. Escalate to the operator"
        ),
    }


def xaacp_seat_incumbent(project_root: Path, *, session_id: str, role: str) -> dict:
    """Who holds this seat, and how long since they last spoke. Read-only."""
    sid = str(session_id or "").strip()
    seat = str(role or "").strip().lower()
    if not sid or not seat:
        return {"ok": False, "status": "invalid", "error": "session_id and role required"}
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT actor_id, host_session_id, host_kind, updated_at "
            "FROM xaacp_actors WHERE session_id=? AND role=?",
            (sid, seat),
        ).fetchone()
    if row is None:
        return {"ok": True, "status": "vacant", "session_id": sid, "role": seat}
    updated_at = float(row["updated_at"] or 0.0)
    return {
        "ok": True,
        "status": "held",
        "session_id": sid,
        "role": seat,
        "actor_id": str(row["actor_id"] or "").strip(),
        "host_session_id": str(row["host_session_id"] or "").strip(),
        "host_kind": str(row["host_kind"] or "").strip(),
        "updated_at": updated_at,
        "idle_seconds": max(0.0, time.time() - updated_at),
    }


# The three things silence can mean. `live` is the only one PROVEN, and it is
# proven by `_xaacp_liveness`'s positive rung alone; `stale` and `silent` differ
# only by the clock. There is deliberately no fourth value for "dead": nothing
# in this system can prove that, and a word for it would get used.
XAACP_SEAT_LIVE = "live"
XAACP_SEAT_STALE = "stale"
XAACP_SEAT_SILENT = "silent"

# The spawn kinds that never hold a seat. A lane and a subagent are the SAME
# THING -- a spawned agent under r0 -- and how the host spawned it is an adapter
# detail carrying no authority.
XAACP_SPAWNED_ACTOR_KINDS = frozenset({"worker", "lane_worker", "subagent"})


def _xaacp_spawned_agent_refusal(route: dict, *, role: str, verb: str) -> dict | None:
    """ONE spelling of "a spawned agent never holds a seat", for every door.

    Entry, succession and release all need this rule and it must be the same
    rule: a descendant that could take, inherit or drop a seat would be acting
    above its own grantor, which the authority ladder forbids.
    """
    kind = str((route or {}).get("actor_kind") or "").strip().lower()
    if kind not in XAACP_SPAWNED_ACTOR_KINDS:
        return None
    return {
        "ok": False,
        "status": "forbidden",
        "error": (
            f"a spawned agent (actor_kind={kind!r}) never holds a seat and cannot "
            f"{verb} one; a seat holder is a conductor-surface actor"
        ),
        "role": str(role or "").strip().lower(),
    }


def _xaacp_seat_grace(grace_seconds: float | None) -> tuple[float, dict | None]:
    """THE grace floor, resolved once. A caller may LENGTHEN it, never shorten.

    Accepting a shorter value would hand any claimant an eviction knob spelled
    `grace_seconds=0`, so the floor is applied here and nowhere else.
    """
    if grace_seconds is None:
        return float(XAACP_SEAT_STALE_GRACE_SECONDS), None
    try:
        grace = float(grace_seconds)
    except (TypeError, ValueError):
        return 0.0, {"ok": False, "status": "invalid", "error": "grace_seconds must be a number"}
    if grace != grace or grace in (float("inf"), float("-inf")):
        return 0.0, {"ok": False, "status": "invalid", "error": "grace_seconds must be finite"}
    return max(grace, float(XAACP_SEAT_STALE_GRACE_SECONDS)), None


def xaacp_seat_occupancy(
    project_root: Path,
    *,
    session_id: str,
    role: str,
    actor_id: str = "",
    grace_seconds: float | None = None,
) -> dict:
    """THE answer to "is this seat occupied, by whom, and is that holder alive".

    ONE LOGIC, ONE HOME. Before this existed, `xaacp_claim_seat` answered the
    question by asking "is there a row" and `xaacp_succeed_seat` answered it by
    asking "is there a row AND has its holder gone quiet past the grace floor".
    Two definitions of the same word, and on 2026-09-12 they disagreed in
    production for one minute straight: co-enter refused `occupied`, succession
    refused `vacant -- claim it instead`, and the two remedies pointed at each
    other. Every seat door now reads THIS function, so the verdicts cannot fork.

    `actor_id`, when given, is the actor ASKING -- never a target. An incumbent
    asking about its own seat gets `self`, because a seat does not block its
    holder.

    Returns `status` in {'invalid', 'vacant', 'self', 'held'}. A held seat also
    carries `liveness` (live | stale | silent -- never "dead", which nothing
    here can prove), the incumbent's idle time, the grace floor in force, an
    `error` fit to show a refused caller, and `next_call`: the ONE call that can
    move this seat from this state. `claimable` and `succeedable` are the two
    booleans the doors act on, so no door re-derives them.
    """
    sid = str(session_id or "").strip()
    seat = str(role or "").strip().lower()
    asker = str(actor_id or "").strip()
    if not sid or not seat:
        return {"ok": False, "status": "invalid", "error": "session_id and role required"}
    grace, bad = _xaacp_seat_grace(grace_seconds)
    if bad is not None:
        return bad

    incumbent = xaacp_seat_incumbent(project_root, session_id=sid, role=seat)
    if not incumbent.get("ok"):
        return incumbent
    if incumbent.get("status") == "vacant":
        return {
            "ok": True,
            "status": "vacant",
            "session_id": sid,
            "role": seat,
            "claimable": True,
            "succeedable": False,
            "next_call": (
                f"ai_seat(mode={'enter' if seat == 'conductor' else 'co-enter'!r}, "
                f"session_id={sid!r})"
            ),
        }
    holder = str(incumbent.get("actor_id") or "").strip()
    if asker and holder == asker:
        return {
            "ok": True,
            "status": "self",
            "session_id": sid,
            "role": seat,
            "actor_id": holder,
            "claimable": True,
            "succeedable": False,
            "next_call": f"ai_seat(mode='exit', role={seat!r}, session_id={sid!r})",
        }

    # A POSITIVE LIVENESS PROOF IS A VETO AND NEVER A GRANT. Absence from the
    # projection is UNKNOWN: it neither proves the incumbent dead nor protects
    # it. Borrowed whole from `_xaacp_liveness`; no second liveness rule here.
    liveness = _xaacp_liveness(project_root, session_id=sid)
    verdict = {}
    if bool(liveness.get("usable")):
        verdict = dict(
            (liveness.get("by_host") or {}).get(
                str(incumbent.get("host_session_id") or "")
            )
            or {}
        )
    proven_live = bool(verdict.get("live"))
    idle = float(incumbent.get("idle_seconds") or 0.0)

    if proven_live:
        state = XAACP_SEAT_LIVE
        remedy = (
            "the incumbent is PROVABLY working, so moving this seat is an authority "
            "transfer only r0 or above may order — escalate to the operator "
            "(a peer's approval can never authorize it)"
        )
    elif idle >= grace:
        state = XAACP_SEAT_STALE
        remedy = (
            f"nothing proves the incumbent live and it has been silent {idle:.0f}s, "
            f"past the {grace:.0f}s grace floor — inherit the seat with "
            f"ai_seat(mode='succeed', role={seat!r}, session_id={sid!r})"
        )
    else:
        state = XAACP_SEAT_SILENT
        remedy = (
            f"the incumbent last spoke {idle:.0f}s ago, inside the {grace:.0f}s grace "
            "period, and nothing proves it gone — unknown is not death. Wait out the "
            f"remaining {max(0.0, grace - idle):.0f}s and then ai_seat(mode='succeed', "
            f"role={seat!r}, session_id={sid!r}), or escalate to the operator"
        )
    return {
        "ok": True,
        "status": "held",
        "session_id": sid,
        "role": seat,
        "actor_id": holder,
        "host_session_id": str(incumbent.get("host_session_id") or ""),
        "host_kind": str(incumbent.get("host_kind") or ""),
        "liveness": state,
        "live_source": str(verdict.get("live_source") or ""),
        "liveness_usable": bool(liveness.get("usable")),
        "updated_at": float(incumbent.get("updated_at") or 0.0),
        "idle_seconds": idle,
        "grace_seconds": grace,
        "claimable": False,
        "succeedable": state == XAACP_SEAT_STALE,
        "next_call": remedy,
        "error": (
            f"XAACP seat {seat!r} is already occupied on session {sid!r} by actor "
            f"{holder!r} (liveness={state}): {remedy}"
        ),
    }


def xaacp_release_seat(
    project_root: Path,
    *,
    session_id: str = "",
    role: str = "",
    reason: str = "",
) -> dict:
    """Give back the CALLER'S OWN seat. There is no way to release another's.

    THE MISSING HALF OF THE LIFECYCLE. A seat could be taken and inherited but
    never handed back: `ai_seat(mode='exit')` cleared only the inline conductor
    marker, so a co-conductor that was finished with a session left its
    authoritative row held and the next conversation had to wait out a six-hour
    grace period to succeed a seat nobody was using.

    NOTE WHAT IS NOT A PARAMETER. No actor, no host session, no "release the
    seat held by X". The holder is derived from `xaacp_resolve_caller_route`,
    the same resolver `ai_msg` uses for sender attribution, so this can only
    ever release the caller's own row. The moment a target became an argument,
    this would be the eviction tool that `succeed` was carefully built not to be.
    """
    seat = str(role or "").strip().lower()
    if seat and seat not in {"conductor", "co_conductor"}:
        return {
            "ok": False,
            "status": "invalid",
            "error": "XAACP seats are conductor/co_conductor only",
        }
    route = xaacp_resolve_caller_route(project_root)
    holder = str(route.get("actor_id") or "").strip()
    if not holder or holder == MSG_ROLE_UNMAPPED:
        return {
            "ok": False,
            "status": "unidentified",
            "error": (
                "the calling actor could not be derived; a seat is released by its "
                "holder and by nobody else"
            ),
        }
    spawned = _xaacp_spawned_agent_refusal(route, role=seat, verb="release")
    if spawned is not None:
        return spawned
    route_session = str(route.get("session_id") or "").strip()
    sid = str(session_id or "").strip() or route_session
    if not sid:
        return {
            "ok": False,
            "status": "invalid",
            "error": "session_id required (and the caller is bound to none)",
        }
    if route_session and route_session != sid:
        return {
            "ok": False,
            "status": "forbidden",
            "error": (
                f"caller is bound to session {route_session!r} and cannot release a "
                f"seat on session {sid!r}"
            ),
        }

    with _connect(project_root) as conn:
        own = conn.execute(
            "SELECT role FROM xaacp_actors WHERE actor_id=? AND session_id=?",
            (holder, sid),
        ).fetchone()
    held = "" if own is None else str(own["role"] or "").strip().lower()
    if not held or (seat and held != seat):
        # TWO DIFFERENT REFUSALS, DELIBERATELY. "I hold nothing" and "somebody
        # else holds that" are different facts, and collapsing them would hide
        # the second one behind a no-op that reads like success.
        target = seat or "conductor/co_conductor"
        occupied_by = ""
        if seat:
            incumbent = xaacp_seat_incumbent(project_root, session_id=sid, role=seat)
            occupied_by = str(incumbent.get("actor_id") or "")
        if occupied_by and occupied_by != holder:
            return {
                "ok": False,
                "status": "forbidden",
                "error": (
                    f"seat {target!r} on session {sid!r} is held by another actor; a "
                    "caller may release only its own seat. If that holder is gone, "
                    f"use ai_seat(mode='succeed', role={target!r}, session_id={sid!r})"
                ),
                "actor_id": holder,
                "incumbent_actor_id": occupied_by,
                "role": target,
            }
        return {
            "ok": False,
            "status": "not_seated",
            "error": f"caller holds no {target} seat on session {sid!r}; nothing released",
            "actor_id": holder,
            "role": target,
        }

    now = time.time()
    try:
        with _connect(project_root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            released = conn.execute(
                "UPDATE xaacp_actors SET role='', updated_at=? "
                "WHERE actor_id=? AND session_id=? AND role=?",
                (now, holder, sid, held),
            )
            if released.rowcount != 1:
                conn.rollback()
                return {
                    "ok": False,
                    "status": "raced",
                    "error": "the seat changed hands while it was being released",
                    "actor_id": holder,
                    "role": held,
                }
            # The legacy projection moves in the SAME transaction, for the same
            # reason succession does: a half-applied release leaves the routing
            # table naming a holder the registry no longer has.
            conn.execute(
                "DELETE FROM msg_role_map WHERE session_id=? AND role=? AND actor_id=?",
                (sid, held, holder),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- a failed release releases nothing
        logger.exception("XAACP seat release failed; the seat is unchanged")
        return {
            "ok": False,
            "status": "error",
            "error": f"seat release rolled back: {type(exc).__name__}: {exc}",
            "actor_id": holder,
            "role": held,
        }

    _audit_event(
        project_root,
        sid,
        action_kind="xaacp_seat_release",
        target_entity=(
            f"{XAACP_SEAT_SUCCESSION_SUBJECT_KIND}:"
            + xaacp_seat_succession_subject(session_id=sid, role=held)
        ),
        payload={
            "session_id": sid,
            "role": held,
            "actor_id": holder,
            "host_session_id": str(route.get("host_session_id") or ""),
            "reason": str(reason or "")[:500],
            "released_at": now,
        },
    )
    return {
        "ok": True,
        "status": "released",
        "actor_id": holder,
        "session_id": sid,
        "role": held,
    }


def xaacp_release_seat_authority(
    project_root: Path,
    *,
    session_id: str = "",
    role: str = "",
    reason: str = "",
) -> dict:
    """Release through the ONE XAACP authority; local SQLite only when unbound."""
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return xaacp_release_seat(
            project_root, session_id=session_id, role=role, reason=reason
        )
    release = getattr(authority, "release_seat", None)
    if not callable(release):
        return {
            "ok": False,
            "status": "unavailable",
            "error": "XAACP authority does not implement seat release",
        }
    return release(
        Path(project_root),
        session_id=str(session_id or ""),
        role=str(role or ""),
        reason=str(reason or ""),
    )


def xaacp_succeed_seat_authority(
    project_root: Path,
    *,
    session_id: str = "",
    role: str = "",
    reason: str = "",
) -> dict:
    """Succeed through the ONE XAACP authority; local SQLite only when unbound.

    THIS WRAPPER IS THE FIX FOR THE MEASURED SPLIT-BRAIN. `co-enter` claimed
    through `xaacp_claim_seat_authority` -- which forwards to the gate on a
    cloud-bound project -- while `succeed` called `xaacp_succeed_seat` directly
    on local SQLite. The gate's registry held the seat, the local copy did not,
    and the two doors reported occupied and vacant about the same seat in the
    same minute. Neither was lying about the store it read; there were two
    stores. Succession now goes where the claim goes, always.
    """
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return xaacp_succeed_seat(
            project_root, session_id=session_id, role=role, reason=reason
        )
    succeed = getattr(authority, "succeed_seat", None)
    if not callable(succeed):
        return {
            "ok": False,
            "status": "unavailable",
            "error": "XAACP authority does not implement seat succession",
        }
    return succeed(
        Path(project_root),
        session_id=str(session_id or ""),
        role=str(role or ""),
        reason=str(reason or ""),
    )


def xaacp_succeed_seat(
    project_root: Path,
    *,
    session_id: str,
    role: str,
    grace_seconds: float | None = None,
    reason: str = "",
) -> dict:
    """Install the CURRENT caller as successor to a seat its holder has left.

    There is deliberately NO parameter naming the successor, the predecessor, or
    the authorizer. The successor is derived from `xaacp_resolve_caller_route`
    -- the same resolver `ai_msg` uses for sender attribution -- and an
    unresolvable caller is nobody, never a default actor. The moment any of
    those became an argument, a caller could install any actor in the
    authoritative seat and the succession would BE the takeover.

    Refuses, loudly and without touching the registry, on every state that is
    not a proven vacancy of duty: `unidentified`, `forbidden` (a spawned agent,
    or a caller bound to another session), `vacant`, `already_seated`,
    `operator_authority_required` (the incumbent is provably live, so only r0 or
    above may order the transfer) and `incumbent_not_proven_dead` (silent, but
    not yet long enough to be evidence -- unknown is not death).
    """
    sid = str(session_id or "").strip()
    seat = str(role or "").strip().lower()
    if seat not in {"conductor", "co_conductor"}:
        return {
            "ok": False,
            "status": "invalid",
            "error": "XAACP seat succession is restricted to conductor/co_conductor",
        }

    # THE GRACE FLOOR. A claimant-supplied grace may only LENGTHEN the silence it
    # must observe. Accepting a shorter one would hand the claimant an eviction
    # knob spelled `grace_seconds=0`.
    grace, bad_grace = _xaacp_seat_grace(grace_seconds)
    if bad_grace is not None:
        return bad_grace

    route = xaacp_resolve_caller_route(project_root)
    successor = str(route.get("actor_id") or "").strip()
    if not successor or successor == MSG_ROLE_UNMAPPED:
        return {
            "ok": False,
            "status": "unidentified",
            "error": (
                "the calling actor could not be derived; a seat successor is "
                "never supplied by the caller"
            ),
        }
    # A SPAWNED AGENT NEVER HOLDS A SEAT, so it never inherits one either. ONE
    # spelling of that rule for every seat door (`_xaacp_spawned_agent_refusal`)
    # -- entry, succession and release -- because a rule with two spellings is
    # a rule that will eventually be true of only one door, which is exactly how
    # the occupancy split-brain above was born.
    spawned = _xaacp_spawned_agent_refusal(route, role=seat, verb="succeed to")
    if spawned is not None:
        return spawned
    route_session = str(route.get("session_id") or "").strip()
    if not sid:
        # AN OMITTED SESSION IS DERIVED, NEVER GUESSED. The caller's own bound
        # session is the only one it could be; an unbound caller gets a refusal
        # rather than a scan for a session to inherit a seat on.
        sid = route_session
    if not sid:
        return {
            "ok": False,
            "status": "invalid",
            "error": "session_id required (and the caller is bound to none)",
        }
    if route_session and route_session != sid:
        # Scope is the security property here as everywhere else in XAACP: a
        # caller bound to one session may not inherit another session's seat.
        return {
            "ok": False,
            "status": "forbidden",
            "error": (
                f"caller is bound to session {route_session!r} and cannot succeed "
                f"a seat on session {sid!r}"
            ),
        }

    # THE ONE OCCUPANCY AUTHORITY, read by this door and by `xaacp_claim_seat`
    # alike. Grading the seat here with a private rule is what made 'occupied'
    # and 'vacant' answerable at the same instant for the same seat.
    occupancy = xaacp_seat_occupancy(
        project_root, session_id=sid, role=seat, actor_id=successor, grace_seconds=grace
    )
    if not occupancy.get("ok"):
        return occupancy
    if occupancy.get("status") == "vacant":
        # NOT A SUCCESSION. Nobody was displaced, so this must not report one --
        # and because the verdict comes from the SAME authority the claim reads,
        # the remedy named here is one that will actually be granted.
        return {
            "ok": False,
            "status": "vacant",
            "error": (
                f"seat {seat!r} on session {sid!r} is unoccupied; claim it instead with "
                + str(occupancy.get("next_call") or "")
            ),
            "role": seat,
            "next_call": occupancy.get("next_call", ""),
        }
    incumbent = occupancy
    predecessor = str(incumbent.get("actor_id") or "")
    if occupancy.get("status") == "self" or predecessor == successor:
        return {
            "ok": True,
            "status": "already_seated",
            "actor_id": successor,
            "session_id": sid,
            "role": seat,
        }

    # The successor must exist in the ONE registry before it can hold a seat in
    # it. Derived values only -- this registration invents no identity, it only
    # records the one the resolver already proved.
    xaacp_register_actor(
        project_root,
        actor_id=successor,
        host_session_id=str(route.get("host_session_id") or "").strip(),
        host_kind=str(route.get("host_kind") or "").strip(),
        session_id=sid,
        actor_kind=str(route.get("actor_kind") or "agent").strip() or "agent",
        source="xaacp_succeed_seat",
        lane_id=str(route.get("lane_id") or "").strip(),
    )

    subject = xaacp_seat_succession_subject(session_id=sid, role=seat)
    # THE GRADE IS THE OCCUPANCY AUTHORITY'S, NOT A SECOND ONE TAKEN HERE.
    liveness_usable = bool(occupancy.get("liveness_usable"))
    proven_live = occupancy.get("liveness") == XAACP_SEAT_LIVE
    idle = float(occupancy.get("idle_seconds") or 0.0)
    verdict = {"live_source": str(occupancy.get("live_source") or "")}

    # THREE CASES, AND ONLY THE MIDDLE ONE IS A SUCCESSION THIS SURFACE CAN GRANT.
    if proven_live:
        # CASE 1 -- the incumbent is PROVABLY working. Moving this seat is an
        # AUTHORITY TRANSFER, not a recovery, and only r0 or above may order one.
        handover = _xaacp_seat_handover_authority(
            project_root, session_id=sid, role=seat, subject=subject
        )
        if not handover.get("ok"):
            return {
                "ok": False,
                "status": handover["status"],
                "error": handover["error"],
                "predecessor_actor_id": predecessor,
                "incumbent_live_source": str(verdict.get("live_source") or ""),
                "approval_standing": bool(handover.get("approval_standing")),
                "role": seat,
            }
        evidence = "operator_handover"
    elif idle >= grace:
        # CASE 2 -- THE DRT-10 CASE. Not provably live AND silent past the grace
        # floor: the dead ChatGPT tab. No approval is required or consulted,
        # because nothing is being taken from anybody.
        evidence = "lease_staleness"
    else:
        # CASE 3 -- NOT PROVABLY LIVE, NOT YET STALE. Unknown is not death, and
        # the answer to "we do not know yet" is to WAIT. This case must never
        # fall through into case 2: silence inside the grace window is the
        # ordinary condition of an idle conversation between operator turns.
        return {
            "ok": False,
            "status": "incumbent_not_proven_dead",
            "error": (
                f"the incumbent of seat {seat!r} last spoke {idle:.0f}s ago, inside "
                f"the {grace:.0f}s grace period, and nothing proves it gone — "
                "unknown is not death. Wait out the grace period, or escalate to "
                "r0 or above for an authorized handover"
            ),
            "predecessor_actor_id": predecessor,
            "incumbent_idle_seconds": idle,
            "grace_seconds": grace,
            "role": seat,
            "next_call": occupancy.get("next_call", ""),
        }
    now = time.time()
    try:
        with _connect(project_root) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # ONE TRANSACTION, BOTH STORES. A half-applied succession leaves the
            # seat owned by nobody or by two, which is strictly worse than the
            # stranding it was meant to cure -- so the authoritative clear, the
            # authoritative install and the legacy projection commit together or
            # not at all.
            still = conn.execute(
                "SELECT actor_id FROM xaacp_actors WHERE session_id=? AND role=?",
                (sid, seat),
            ).fetchone()
            if still is None or str(still["actor_id"] or "").strip() != predecessor:
                conn.rollback()
                return {
                    "ok": False,
                    "status": "raced",
                    "error": "the seat changed hands while this succession was being graded",
                    "predecessor_actor_id": predecessor,
                    "role": seat,
                }
            conn.execute(
                "UPDATE xaacp_actors SET role='', updated_at=? "
                "WHERE actor_id=? AND session_id=?",
                (now, predecessor, sid),
            )
            # The successor drops whatever seat it held first, for the same
            # reason `xaacp_claim_seat` does: the unique index permits one seat
            # per actor-session and a stale one would block the install.
            conn.execute(
                "UPDATE xaacp_actors SET role='' WHERE actor_id=? AND session_id=?",
                (successor, sid),
            )
            installed = conn.execute(
                "UPDATE xaacp_actors SET role=?, updated_at=? "
                "WHERE actor_id=? AND session_id=?",
                (seat, now, successor, sid),
            )
            if installed.rowcount != 1:
                conn.rollback()
                return {
                    "ok": False,
                    "status": "forbidden",
                    "error": "successor actor is not registered on this session",
                    "actor_id": successor,
                    "role": seat,
                }
            conn.execute(
                "DELETE FROM msg_role_map WHERE session_id=? AND role=?",
                (sid, seat),
            )
            successor_host = str(route.get("host_session_id") or "").strip()
            if successor_host:
                conn.execute(
                    "INSERT OR REPLACE INTO msg_role_map "
                    "(host_session_id, role, actor_id, session_id, host_kind, "
                    "updated_at, source) VALUES (?,?,?,?,?,?,?)",
                    (
                        successor_host,
                        seat,
                        successor,
                        sid,
                        str(route.get("host_kind") or "").strip(),
                        now,
                        MSG_SEAT_SOURCE_SUCCESSION,
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- a failed transfer transfers nothing
        logger.exception("XAACP seat succession failed; the seat is unchanged")
        return {
            "ok": False,
            "status": "error",
            "error": f"seat succession rolled back: {type(exc).__name__}: {exc}",
            "predecessor_actor_id": predecessor,
            "role": seat,
        }

    # WHO SUCCEEDED WHOM, WHEN, ON WHAT EVIDENCE. Written after the commit so the
    # audit never describes a transfer that did not happen.
    _audit_event(
        project_root,
        sid,
        action_kind="xaacp_seat_succession",
        target_entity=f"{XAACP_SEAT_SUCCESSION_SUBJECT_KIND}:{subject}",
        payload={
            "session_id": sid,
            "role": seat,
            "predecessor_actor_id": predecessor,
            "predecessor_host_session_id": str(incumbent.get("host_session_id") or ""),
            "successor_actor_id": successor,
            "successor_host_session_id": str(route.get("host_session_id") or ""),
            "evidence": evidence,
            "incumbent_idle_seconds": idle,
            "incumbent_last_seen_at": float(incumbent.get("updated_at") or 0.0),
            "grace_seconds": grace,
            "incumbent_proven_live": proven_live,
            "liveness_usable": liveness_usable,
            "approval_subject": f"{XAACP_SEAT_SUCCESSION_SUBJECT_KIND}:{subject}",
            "reason": str(reason or "")[:500],
            "succeeded_at": now,
        },
    )
    return {
        "ok": True,
        "status": "succeeded",
        "actor_id": successor,
        "predecessor_actor_id": predecessor,
        "session_id": sid,
        "role": seat,
        "evidence": evidence,
        "incumbent_idle_seconds": idle,
        "grace_seconds": grace,
    }


def _xaacp_liveness(project_root: Path, *, session_id: str) -> dict:
    """THE liveness projection for XAACP, borrowed whole from `agent_audit`.

    Local backlog 987. One oracle, one home. The audit owns every positive rung:
    current caller, shared current-generation XAACP presence, and live binder PID.
    This adapter supplies request context and fails closed; it never invents its
    own actor-death or timeout rule.

    FAILS CLOSED, LOUDLY. If the audit cannot be read, no actor has been PROVEN
    live, so none is addressable and `roster_status` says the roster is
    unavailable. Failing open would restore the exact defect: every historical
    row addressable again, silently. Failing closed without saying so would be
    worse still — an empty addressable set that reads like "nobody is here".
    """
    try:
        from .agent_audit import liveness_by_host_session
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        try:
            caller = str(current_calling_host_session_id() or "")
        except Exception:  # noqa: BLE001 — no caller context is not a failure
            caller = ""
        out = dict(
            liveness_by_host_session(
                project_root, session_id=session_id, caller_host_session_id=caller
            )
        )
        # USABLE means the question was ANSWERED. Without it, an empty `by_host`
        # from a failed read is indistinguishable from an empty one from a
        # project where nothing is bound yet — and those grade OPPOSITELY.
        out["usable"] = True
        return out
    except Exception:  # noqa: BLE001
        logger.exception("XAACP liveness projection unavailable")
        return {
            "by_host": {},
            "usable": False,
            "roster_status": "unavailable",
            "roster_status_reason": (
                "the agent liveness audit could not be read, so NO actor could be "
                "proven live — this roster lists no addressable actors, which does "
                "NOT mean nobody is connected"
            ),
        }


def xaacp_directory(project_root: Path, *, session_id: str) -> dict:
    """Session-scoped roster of XAACP actors, with HONEST addressability (#54).

    XAACP had a transport and NO DIRECTORY: delivery worked when the caller
    already knew the address, and nothing let a participant discover one. This
    is the discovery surface, governed by the memory war's law: nothing surfaces
    without a handle the surfacing tool itself can resolve.

    ADDRESSABILITY IS NOW DERIVED, NOT ASSERTED (local backlog 987). Every
    historical row used to be emitted `addressable: True` unconditionally, so
    the roster grew a ghost per deploy and per reconnect — each generation mints
    a new actor_id from a new host_session_id, and nothing ever superseded the
    old one. MEASURED on the gate: this surface offered 7 addressable actors
    while `ai_agents` independently reported 2 live + 5 unverifiable FROM THE
    SAME STORE. Two tools, one question, two answers.

    THE LIVENESS LADDER IS BORROWED, NEVER RE-INVENTED. `agent_audit
    .liveness_by_host_session` is the one oracle; nothing here probes anything.
    That matters for what it does NOT do: pid is used only positively there, so
    a failed probe is UNVERIFIABLE and never dead (#603). No rung concludes an
    actor died, which is what keeps this from reintroducing the actor-death
    predicate `dd19b8b40` removed — and there is no TTL, which would be
    pid-death wearing a clock.

    HISTORY IS PRESERVED. An unverifiable actor stays LISTED, carrying
    `addressable: False` with `live_source`/`addressable_reason` saying why. So
    the total roster may legitimately grow; what may not grow is the ADDRESSABLE
    count. `roster_status` reports when unverifiable rows make this roster
    incomplete, so an operator never reads a partial answer as authoritative.

    Being listed confers NO authority — these are address handles, not grants.
    And addressability here is PRESENTATION of the same truth
    `_xaacp_resolve_target_route` enforces; a caller who retained an old
    actor_id is refused there, not merely un-advertised here.

    Scope is the security property: ONE session's actors, never a tenant,
    project, or sibling-session leak. An empty session answers an honest empty,
    never an invented actor.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "status": "invalid", "error": "session_id required"}

    liveness = _xaacp_liveness(project_root, session_id=sid)
    by_host = liveness["by_host"]
    usable = bool(liveness.get("usable"))

    def _grade(host_session_id: str) -> tuple[bool, str, str]:
        """(addressable, live_source, reason) for one actor's host session.

        A host session ABSENT from the projection is UNKNOWN, and unknown is not
        live — a miss must never read as permission.
        """
        if not usable:
            # THE QUESTION WENT UNANSWERED. Not "not yet bound" — the branch
            # below would grant every actor on an empty `by_host`, turning a
            # failed read into a blanket grant. Nothing is proven, so nothing is
            # offered.
            return (
                False,
                "",
                (
                    "the agent liveness audit could not be read, so this actor's "
                    "liveness is unknown — this is NOT a finding that it is gone"
                ),
            )
        hsid = str(host_session_id or "").strip()
        verdict = by_host.get(hsid) if hsid else None
        if verdict is not None:
            if verdict.get("live"):
                return True, str(verdict.get("live_source") or ""), ""
            # PRESENT AND UNPROVEN — this is the ghost. The binding exists, so
            # the actor was bound once and a later generation superseded it in
            # practice without superseding it in the table. THIS is what stops
            # being addressable, and it is exactly the population `ai_agents`
            # already counts as unverifiable.
            return False, "", str(verdict.get("reason") or "liveness could not be verified")
        # ABSENT ENTIRELY — a different state, and NOT a ghost.
        #
        # `xaacp_enter_seat` says so in its own docstring: "Seat entry may be the
        # operation that ESTABLISHES local managed-mode state, so it cannot
        # require an already-seated/managed XAACP route." An actor can therefore
        # legitimately hold no conductor binding, and refusing it here broke a
        # provably-live caller sending to its own seat.
        #
        # A ghost cannot reach this branch: it was bound once, and `dd19b8b40`
        # removed every automatic deletion of a binding on unprovable liveness —
        # only an explicit self-unbind or an admin mass-unbind removes one, and
        # both are deliberate operator acts. So "no binding" means not-yet-bound,
        # never a superseded generation.
        #
        # Labelled rather than silently permitted: the reader is told this is an
        # assumption, not a proof.
        return (
            True,
            "unbound_actor",
            (
                "no conductor binding exists for this actor yet (seat entry may "
                "precede managed-mode state); liveness is ASSUMED, not proven"
            ),
        )

    actors: list[dict] = []
    try:
        from .session_lane_agents_store import SessionLaneAgentsStore

        for row in SessionLaneAgentsStore().get_all_lane_agents(project_root):
            if str(row.get("session_id") or "").strip() != sid:
                continue
            state = str(row.get("state") or "").strip()
            running = state == "running"
            actors.append(
                {
                    "actor_kind": "worker",
                    "actor_id": str(row.get("worker_id") or "").strip(),
                    "lane_id": str(row.get("lane_id") or "").strip(),
                    "session_id": sid,
                    "role": "",
                    "state": state,
                    "backend": str(row.get("backend") or "").strip(),
                    "addressable": running,
                    # A worker reports its OWN lifecycle, so its state IS the
                    # evidence — unlike an agent binding, whose stamp belongs to
                    # the server that wrote it.
                    "live_source": "lane_state" if running else "",
                    "addressable_reason": "" if running else f"lane worker state is {state or 'unknown'!r}",
                },
            )
    except Exception:
        logger.exception("XAACP directory worker roster read failed")
        return {
            "ok": False,
            "status": "error",
            "error": "lane registry unreadable; refusing a partial directory",
        }

    try:
        from .mcp_server_runtime_helpers import current_gate_principal

        _directory_on_gate = bool(current_gate_principal())
    except Exception:
        _directory_on_gate = False
    if _directory_on_gate:
        seat_rows = []
    else:
        with _connect(project_root) as conn:
            seat_rows = conn.execute(
                "SELECT role, actor_id, session_id, host_session_id FROM msg_role_map "
                "WHERE session_id = ? AND actor_id != '' ORDER BY role",
                (sid,),
            ).fetchall()
    for row in seat_rows:
        role = str(row["role"] or "").strip()
        if role not in MSG_ROLES:
            continue
        ok, src, why = _grade(str(row["host_session_id"] or ""))
        actors.append(
            {
                "actor_kind": "seat",
                "actor_id": str(row["actor_id"] or "").strip(),
                "lane_id": "",
                "session_id": sid,
                "role": role,
                "state": "",
                "backend": "",
                "addressable": ok,
                "live_source": src,
                "addressable_reason": why,
            },
        )

    # Bound top-level agents are listed even when they hold no seat. This is the
    # cross-surface case (web/local/remote/server conductor identities): a handle
    # is a mailbox address, not a role grant. Seats/workers already listed above
    # win presentation when the same actor also has one of those shapes.
    existing_actor_ids = {str(a.get("actor_id") or "") for a in actors}
    with _connect(project_root) as conn:
        agent_rows = conn.execute(
            "SELECT actor_id, host_session_id, host_kind, actor_kind, role, updated_at, "
            "host_agent_id, lane_id "
            "FROM xaacp_actors WHERE session_id=? ORDER BY updated_at DESC",
            (sid,),
        ).fetchall()
    for row in agent_rows:
        actor_id = str(row["actor_id"] or "").strip()
        if not actor_id or actor_id in existing_actor_ids:
            continue
        # #1007: a CC subagent is listed as ITS OWN actor. It shares its
        # parent's host_session_id (so its liveness IS the parent window's)
        # and is told apart by host_agent_id, the host-issued per-subagent id.
        ok, src, why = _grade(str(row["host_session_id"] or ""))
        actors.append(
            {
                "actor_kind": "seat" if str(row["role"] or "").strip() else (str(row["actor_kind"] or "agent").strip() or "agent"),
                "actor_id": actor_id,
                "lane_id": str(row["lane_id"] or "").strip(),
                "session_id": sid,
                "role": str(row["role"] or "").strip(),
                "state": "",
                "backend": str(row["host_kind"] or "").strip(),
                "host_session_id": str(row["host_session_id"] or "").strip(),
                "host_agent_id": str(row["host_agent_id"] or "").strip(),
                "addressable": ok,
                "live_source": src,
                "addressable_reason": why,
            }
        )
        existing_actor_ids.add(actor_id)

    addressable = [a for a in actors if a.get("addressable")]
    return {
        "ok": True,
        "session_id": sid,
        "actors": actors,
        # The counts an operator actually compares. `actors` may legitimately
        # grow as history accumulates; `addressable_count` may not.
        "actor_count": len(actors),
        "addressable_count": len(addressable),
        "unverifiable_count": len(actors) - len(addressable),
        "roster_status": liveness["roster_status"],
        "roster_status_reason": liveness["roster_status_reason"],
    }


def xaacp_authority_for(project_root: Path):
    """Return the single XAACP authority for this project.

    Local/unbound projects keep this module's SQLite state. Cloud-bound clients
    forward to the authenticated gate; gate-side execution returns ``None`` and
    uses the canonical server copy directly, so there is never a forwarding loop.
    """
    from .xaacp_authority import remote_authority_for

    return remote_authority_for(Path(project_root))


#: #1061 LIVE FAILURE 2026-09-14 ~01:02Z: two forwarded parks (300s, 120s) both
#: died after >120s with "xaacp hub unreachable: TimeoutError" while a message
#: sat in the parker's inbox. One HTTP request held open for the whole park
#: outlives what the transport path allows. A forwarded park is therefore a
#: SEQUENCE of short hub waits, each well under the transport budget, resuming
#: from the returned cursor -- the park survives the transport and wakes on
#: the first slice that sees the delivery.
XAACP_REMOTE_PARK_SLICE_SECONDS = 20.0
#: Consecutive transport failures tolerated inside one park before the park
#: reports the transport failure (a dead hub must still be named, not hidden).
XAACP_REMOTE_PARK_MAX_TRANSPORT_FAILURES = 3


def _xaacp_remote_park(
    project_root: Path,
    authority,
    payload: dict,
    *,
    metadata: dict | None,
    after_cursor: int,
    timeout_seconds: float,
    clock=time.monotonic,
) -> dict:
    try:
        total = max(0.0, min(float(timeout_seconds or 0.0), 300.0))
    except (TypeError, ValueError):
        total = 0.0
    deadline = clock() + total
    cursor = after_cursor
    failures = 0
    previous_timeout = getattr(authority, "_timeout", None)
    last: dict = {}
    try:
        while True:
            remaining = max(0.0, deadline - clock())
            slice_wait = min(remaining, XAACP_REMOTE_PARK_SLICE_SECONDS)
            sliced = dict(payload)
            sliced["timeout_seconds"] = slice_wait
            sliced["after_cursor"] = cursor
            # Cross-surface compatibility: the remote bridge forwards metadata
            # but predates the explicit after_cursor argument.
            sliced["metadata"] = {
                **(metadata if isinstance(metadata, dict) else {}),
                "_wait_next_after_cursor": cursor,
            }
            if previous_timeout is not None:
                setattr(authority, "_timeout", max(float(previous_timeout), slice_wait + 10.0))
            last = authority.dispatch(project_root, **sliced)
            status = str((last or {}).get("status") or "")
            if last.get("ok") is not False and status != "timeout":
                return last
            if last.get("ok") is False:
                transport = status == "unavailable" and "unreachable" in str(last.get("error") or "")
                if not transport:
                    return last  # a refusal is an answer, never retried
                failures += 1
                if failures >= XAACP_REMOTE_PARK_MAX_TRANSPORT_FAILURES:
                    return {**last, "cursor": cursor, "park_transport_failures": failures}
            else:
                failures = 0
                try:
                    cursor = max(int(cursor or 0), int(last.get("cursor") or 0))
                except (TypeError, ValueError):
                    pass
            if clock() >= deadline:
                return {
                    "ok": True,
                    "status": "timeout",
                    "cursor": cursor,
                    "message": None,
                    **(
                        {"durable_receive_cursor": last["durable_receive_cursor"]}
                        if isinstance(last, dict) and "durable_receive_cursor" in last
                        else {}
                    ),
                }
    finally:
        if previous_timeout is not None:
            setattr(authority, "_timeout", previous_timeout)


def xaacp_dispatch(
    project_root: Path,
    *,
    mode: str,
    session_id: str = "",
    target_actor_id: str = "",
    lane_id: str = "",
    message_kind: str = "",
    body: str = "",
    message_id: str = "",
    correlation_id: str = "",
    reply_to_id: str = "",
    decision: str = "",
    timeout_seconds: float = 0.0,
    after_cursor: int = 0,
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
    wake: bool = False,
    metadata: dict | None = None,
    ttl_seconds: float | None = None,
    confirm_token: str = "",
    reason: str = "",
) -> dict:
    authority = xaacp_authority_for(project_root)
    payload = {
        "mode": mode,
        "session_id": session_id,
        "target_actor_id": target_actor_id,
        "lane_id": lane_id,
        "message_kind": message_kind,
        "body": body,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "reply_to_id": reply_to_id,
        "decision": decision,
        "timeout_seconds": timeout_seconds,
        "after_cursor": after_cursor,
        "unread_only": unread_only,
        "mark_read": mark_read,
        "limit": limit,
        "wake": wake,
        "metadata": metadata,
        "ttl_seconds": ttl_seconds,
        "confirm_token": confirm_token,
        "reason": reason,
    }
    if authority is not None:
        if str(mode or "").strip().lower() == "wait_next":
            return _xaacp_remote_park(
                project_root,
                authority,
                payload,
                metadata=metadata,
                after_cursor=after_cursor,
                timeout_seconds=timeout_seconds,
            )
        return authority.dispatch(project_root, **payload)
    return _xaacp_dispatch_local(project_root, **payload)


def _xaacp_dispatch_local(
    project_root: Path,
    *,
    mode: str,
    session_id: str = "",
    target_actor_id: str = "",
    lane_id: str = "",
    message_kind: str = "",
    body: str = "",
    message_id: str = "",
    correlation_id: str = "",
    reply_to_id: str = "",
    decision: str = "",
    timeout_seconds: float = 0.0,
    after_cursor: int = 0,
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
    wake: bool = False,
    metadata: dict | None = None,
    ttl_seconds: float | None = None,
    confirm_token: str = "",
    reason: str = "",
) -> dict:
    """Dispatch XAACP through the caller's canonical actor/session route."""
    selected = str(mode or "").strip().lower()
    if selected not in _XAACP_MODES:
        return {"ok": False, "status": "invalid", "error": "unknown XAACP mode"}
    forwarded_op = metadata.get(_STREAM_OP_KEY) if isinstance(metadata, dict) else None
    if (
        selected == "wait_next"
        and isinstance(forwarded_op, dict)
        and forwarded_op.get("op") == "operator_append"
    ):
        # Operator append is NOT an actor operation: no caller route is used.
        # Authority comes ONLY from the gate-resolved principal (checked inside);
        # an agent without an org-admin principal is refused.
        return _xaacp_operator_append(
            project_root,
            _allow_local_process=False,
            session_id=session_id,
            target_actor_id=str(forwarded_op.get("target_actor_id") or ""),
            body=str(forwarded_op.get("body") or ""),
            causal_ref=str(forwarded_op.get("causal_ref") or ""),
            operator_principal=forwarded_op.get("operator_principal")
            if isinstance(forwarded_op.get("operator_principal"), dict)
            else {},
            transport=str(forwarded_op.get("transport") or ""),
        )
    route = xaacp_resolve_caller_route(project_root)
    actor_id = str(route.get("actor_id") or "").strip()
    caller_session_id = str(route.get("session_id") or "").strip()
    requested_session_id = str(session_id or "").strip()
    if not actor_id or not caller_session_id:
        # #1001: name the binding that is missing and how to make one. The
        # session comes from the caller's managed binding and nowhere else --
        # never a gate selection, never a healed singleton -- so an unbound
        # caller is told that, not handed someone else's answer.
        hsid = str(route.get("host_session_id") or "").strip() or str(
            _xaacp_current_host_session_id() or ""
        ).strip()
        return {
            "ok": False,
            "status": "forbidden",
            "error": "caller has no canonical XAACP actor/session binding",
            "host_session_id": hsid,
            "missing_binding_host_session_id": hsid,
            "remedy": (
                f"ai_session(mode='connect', session_id='{requested_session_id}')"
                if requested_session_id
                else "ai_session(mode='connect')"
            ),
        }
    if requested_session_id != caller_session_id:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "XAACP session_id does not match the caller's bound session",
            "bound_session_id": caller_session_id,
            "resolved_via": "managed_binding",
        }
    if str(route.get("actor_kind") or "") != "worker":
        try:
            from .mcp_server_runtime_helpers import current_calling_host_kind

            xaacp_register_actor(
                project_root,
                actor_id=actor_id,
                host_session_id=str(route.get("host_session_id") or "").strip(),
                host_kind=str(current_calling_host_kind() or "").strip(),
                session_id=requested_session_id,
                actor_kind=str(route.get("actor_kind") or "agent").strip() or "agent",
                # #1007: a subagent carries the host-issued id that separates
                # it from the conductor sharing its host_session_id.
                host_agent_id=str(route.get("host_agent_id") or "").strip(),
            )
        except Exception:
            logger.exception("XAACP caller actor registration failed")
            return {
                "ok": False,
                "status": "error",
                "error": "XAACP actor registry unavailable; refusing partial routing",
            }
    consul_op = metadata.get(_STREAM_OP_KEY) if isinstance(metadata, dict) else None
    if selected == "wait_next" and isinstance(consul_op, dict) and consul_op.get("op") in {
        "seat_of_host", "turn_mirror", "last_park",
    }:
        # #1061 consul-stop ops. Identity is THIS dispatcher's derived route:
        # the host session and actor are never payload values.
        caller_hsid = str(route.get("host_session_id") or "").strip()
        if consul_op["op"] == "seat_of_host":
            return _seat_of_host_local(project_root, session_id=requested_session_id, host_session_id=caller_hsid)
        if consul_op["op"] == "turn_mirror":
            return _turn_mirror_local(
                project_root, session_id=requested_session_id, host_session_id=caller_hsid,
                body=str(consul_op.get("body") or ""), sha256=str(consul_op.get("sha256") or ""),
                pointer=str(consul_op.get("pointer") or ""),
                causal_turn_id=str(consul_op.get("causal_turn_id") or ""),
            )
        return _last_park_local(project_root, session_id=requested_session_id, actor_id=actor_id)
    if selected == "xaacp_directory":
        # Read-only discovery; scoped to the caller's own bound session by
        # the check above. Listing confers no authority (#54).
        return xaacp_directory(project_root, session_id=requested_session_id)

    supplied_target = str(target_actor_id or "").strip()
    if selected != "xaacp_send" and supplied_target and supplied_target != actor_id:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "target_actor_id cannot override the canonical caller actor",
        }
    if selected == "xaacp_send":
        resolution = xaacp_resolve_send_target(
            project_root,
            session_id=requested_session_id,
            target_actor_id=supplied_target,
            lane_id=lane_id,
        )
        if not resolution.get("ok"):
            return resolution
        target_route = resolution["route"]
        canonical_target = str(target_route.get("actor_id") or "").strip()
        return xaacp_send(
            project_root,
            target_route_state=str(resolution.get("route_state") or "live"),
            session_id=requested_session_id,
            # SENDER IS DERIVED, NEVER SUPPLIED (#1061 security floor): the
            # caller's own canonical route; no ai_msg parameter reaches here.
            sender_actor_id=actor_id,
            sender_actor_kind=str(route.get("actor_kind") or ""),
            target_actor_id=canonical_target,
            lane_id=lane_id,
            message_kind=message_kind,
            body=body,
            correlation_id=correlation_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
            wake=wake,
            ttl_seconds=ttl_seconds,
        )
    if selected in {"xaacp_inbox", "wait_next"}:
        caller_lane_id = str(route.get("lane_id") or "").strip()
        requested_lane_id = str(lane_id or "").strip()
        if route.get("actor_kind") == "worker" and requested_lane_id != caller_lane_id:
            return {
                "ok": False,
                "status": "forbidden",
                "error": "worker XAACP inbox is restricted to its bound lane",
                "bound_lane_id": caller_lane_id,
            }
        stream_op = metadata.get(_STREAM_OP_KEY) if isinstance(metadata, dict) else None
        if selected == "wait_next" and stream_op is not None:
            # #1061 rework: the forwarded two-phase claim/confirm. The reader is
            # THIS dispatcher's derived caller actor -- never a payload value.
            if not isinstance(stream_op, dict) or stream_op.get("op") not in {"claim", "confirm"}:
                return {"ok": False, "status": "invalid", "error": "unknown stream op"}
            if stream_op["op"] == "claim":
                via = str(stream_op.get("surface") or "").strip()
                if not via or via == "wait_next":
                    return {"ok": False, "status": "invalid", "error": "a claim needs a non-wait_next surface"}
                try:
                    op_limit = int(stream_op.get("limit") or 1)
                except (TypeError, ValueError):
                    op_limit = 1
                return _stream_claim_local(
                    project_root,
                    session_id=requested_session_id,
                    actor_id=actor_id,
                    lane_id=requested_lane_id,
                    reader_actor_kind=str(route.get("actor_kind") or ""),
                    surface=via,
                    limit=op_limit,
                )
            raw_ids = stream_op.get("claim_ids")
            return _stream_confirm_local(
                project_root,
                session_id=requested_session_id,
                actor_id=actor_id,
                claim_ids=[str(c) for c in raw_ids] if isinstance(raw_ids, list) else [],
            )
        if selected == "wait_next":
            effective_cursor = after_cursor
            if isinstance(metadata, dict) and "_wait_next_after_cursor" in metadata:
                effective_cursor = metadata["_wait_next_after_cursor"]
            return xaacp_wait_next(
                project_root,
                session_id=requested_session_id,
                target_actor_id=actor_id,
                lane_id=requested_lane_id,
                reader_actor_kind=str(route.get("actor_kind") or ""),
                after_cursor=effective_cursor,
                timeout_seconds=timeout_seconds,
            )
        return xaacp_inbox(
            project_root,
            session_id=requested_session_id,
            target_actor_id=actor_id,
            lane_id=requested_lane_id,
            reader_actor_kind=str(route.get("actor_kind") or ""),
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
        )
    if selected == "xaacp_reply":
        return xaacp_reply(
            project_root,
            message_id=message_id,
            session_id=requested_session_id,
            responder_actor_id=actor_id,
            decision=decision,
            body=body,
        )
    if selected == "xaacp_ack":
        # NO IDENTITY ARGUMENT CROSSES THIS CALL. `xaacp_ack` re-resolves the
        # caller through the same resolver this dispatcher already used, so
        # there is no parameter anywhere by which a caller could name the acker
        # -- not even an internal one a future surface could be talked into
        # forwarding. `body` is the optional ACK text, not a decision.
        return xaacp_ack(
            project_root,
            message_id=message_id,
            session_id=requested_session_id,
            body=body,
        )
    if selected == "xaacp_wait":
        return xaacp_wait(
            project_root,
            message_id=message_id,
            session_id=requested_session_id,
            actor_id=actor_id,
            timeout_seconds=timeout_seconds,
        )
    return xaacp_cancel(
        project_root,
        message_id=message_id,
        session_id=requested_session_id,
        sender_actor_id=actor_id,
        reason=reason,
    )


#: #1061 (d): the role-addressed CHANNEL is retired; these modes are thin views
#: over the ONE actor stream for one compatibility release.
LEGACY_ROLE_MODES_DEPRECATION = (
    "ai_msg send/inbox/reply are deprecated compatibility views over the XAACP "
    "actor stream (#1061) and will be removed next release; use xaacp_send "
    "(target_actor_id may be a role alias such as 'conductor'), xaacp_inbox "
    "and wait_next"
)


def _legacy_role_view(
    project_root: Path,
    *,
    mode: str,
    from_role: str,
    to_roles: object,
    body: str,
    in_reply_to: str,
    message_id: str,
    unread_only: bool,
    mark_read: bool,
    limit: int,
) -> dict:
    """Legacy role send/inbox/reply as VIEWS -- no queue, no read state of their own.

    send  -> one xaacp_send per role alias (resolved at send time to the
             current seat holder of the caller's bound session);
    inbox -> xaacp_inbox of the caller's own actor (its read state IS the
             stream's), plus pre-#1061 role-channel rows as READ-ONLY
             `legacy_history` (never written to, so no second read state);
    reply -> xaacp_send in_reply_to the other participant.
    """
    route = xaacp_resolve_caller_route(project_root)
    session_id = str(route.get("session_id") or "").strip()
    common = {"role": from_role, "deprecated": LEGACY_ROLE_MODES_DEPRECATION}
    if not session_id:
        return {
            **common,
            "sent": False,
            "ok": False,
            "status": "forbidden",
            "reason": (
                "caller has no bound session, and role aliases resolve only inside "
                "the caller's own bound session; ai_session(mode='connect') first"
            ),
            **({"messages": []} if mode == "inbox" else {}),
        }
    if mode == "inbox":
        out = xaacp_dispatch(
            project_root,
            mode="xaacp_inbox",
            session_id=session_id,
            lane_id="",
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
        )
        legacy = [
            {**item, "legacy": True}
            for item in msg_inbox(
                project_root, role=from_role, unread_only=unread_only, mark_read=False
            )
        ]
        return {**common, **out, "messages": list(out.get("messages") or []), "legacy_history": legacy}

    targets: list[str]
    reply_to = ""
    if mode == "reply":
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT direction, session_id, from_role, to_roles_json, sender_actor_id, "
                "target_actor_id FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        if not row:
            return {**common, "sent": False, "reason": f"Message '{message_id}' not found"}
        caller = str(route.get("actor_id") or "").strip()
        if row["direction"] == "xaacp":
            if str(row["session_id"] or "") != session_id:
                return {**common, "sent": False, "status": "not_found", "reason": f"Message '{message_id}' not found"}
            other = (
                str(row["sender_actor_id"] or "")
                if caller == str(row["target_actor_id"] or "")
                else str(row["target_actor_id"] or "")
            )
            targets, reply_to = [other], message_id
        else:
            try:
                listed = json.loads(row["to_roles_json"] or "[]")
            except (TypeError, ValueError):
                listed = []
            targets = []
            for role in [row["from_role"], *listed]:
                if role and role != from_role and role not in targets:
                    targets.append(role)
    else:
        targets = _normalize_to_roles(to_roles)
    results = []
    for target in targets:
        results.append(
            {
                "target": target,
                **xaacp_dispatch(
                    project_root,
                    mode="xaacp_send",
                    session_id=session_id,
                    target_actor_id=target,
                    lane_id="",
                    message_kind="message",
                    body=body,
                    reply_to_id=reply_to,
                    metadata={
                        "legacy_surface": f"ai_msg.{mode}",
                        "from_role": from_role,
                        "to_roles": targets,
                        **({"legacy_in_reply_to": message_id} if mode == "reply" and not reply_to else {}),
                    },
                ),
            }
        )
    sent = [r for r in results if r.get("ok")]
    first = sent[0] if sent else {}
    return {
        **common,
        "sent": bool(results) and len(sent) == len(results),
        "id": first.get("message_id", ""),
        "thread_id": first.get("correlation_id", ""),
        "to_roles": targets,
        "results": results,
    }


def ai_msg_dispatch(
    project_root: Path,
    *,
    mode: str,
    to_roles: str = "",
    body: str = "",
    in_reply_to: str = "",
    message_id: str = "",
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
    session_id: str = "",
    target_actor_id: str = "",
    lane_id: str = "",
    message_kind: str = "",
    correlation_id: str = "",
    decision: str = "",
    timeout_seconds: float = 0.0,
    after_cursor: int = 0,
    wake: bool = False,
    metadata: dict | None = None,
    ttl_seconds: float | None = None,
    confirm_token: str = "",
    reason: str = "",
) -> dict:
    """Canonical behavior behind the public ai_msg handler."""
    selected = str(mode or "").strip().lower()
    if selected in _XAACP_MODES:
        return xaacp_dispatch(
            project_root,
            mode=selected,
            session_id=session_id,
            target_actor_id=target_actor_id,
            lane_id=lane_id,
            message_kind=message_kind,
            body=body,
            message_id=message_id,
            correlation_id=correlation_id,
            reply_to_id=in_reply_to,
            decision=decision,
            timeout_seconds=timeout_seconds,
            after_cursor=after_cursor,
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
            wake=wake,
            metadata=metadata,
            ttl_seconds=ttl_seconds,
            confirm_token=confirm_token,
            reason=reason,
        )

    from_role = msg_resolve_caller_role(project_root)
    if from_role not in MSG_ROLES:
        if selected == "inbox":
            return {"role": from_role, "messages": []}
        if selected in {"send", "reply"}:
            # #640 specimen 2 — THE REMEDY TEXT WAS THE BUG. This refusal used
            # to end "Bind first via ai_seat(mode='enter')." A lane that OBEYS
            # that remedy SEIZES the sitting conductor's seat, so the sentence
            # meant to unblock a caller was an instruction to commit a coup.
            # VOCABULARY RULE: a refusal whose stated cause or remedy is wrong
            # is its own defect, independent of the refusal being correct.
            # The refusal IS correct — a non-seat caller must not post as a
            # seat. What it owed the caller was a channel the caller may
            # actually use. Both named below are open to a lane with no seat
            # and (post-#650) no task.
            return {
                "sent": False,
                "reason": (
                    f"caller is not a bound seat (role={from_role!r}); only the "
                    "conductor / co_conductor / king may post or read seat "
                    "messages. This is not a bind problem and you must NOT "
                    "enter a seat to fix it — a seat is occupied by its host, "
                    "and taking it would evict the sitting conductor. To "
                    "report upward from here: ai_issues(mode='file', "
                    "content=..., confirm='file-issue') for a refusal report "
                    "(needs no seat and no task), or ai_msg(mode='xaacp_send', "
                    "session_id=<your session>, target_actor_id='conductor') "
                    "for actor-routed delivery to the conductor of your own "
                    "session."
                ),
                "remedy_channels": ["ai_issues", "ai_msg:xaacp_send"],
            }
    if selected in {"send", "inbox", "reply"}:
        return _legacy_role_view(
            project_root,
            mode=selected,
            from_role=from_role,
            to_roles=to_roles,
            body=body,
            in_reply_to=in_reply_to,
            message_id=message_id,
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
        )
    # Enumerated literally (not joined from _XAACP_MODES) on purpose: this
    # string is the human-readable dispatch truth for the modes this function
    # forwards, and #54's xaacp_directory was missing from it — a caller who
    # typo'd was told the directory mode does not exist while it did.
    valid = (
        "send|inbox|reply|xaacp_send|xaacp_inbox|xaacp_ack|xaacp_reply|"
        "xaacp_wait|xaacp_cancel|xaacp_directory|wait_next"
    )
    return {"error": f"unknown mode: {mode!r} (valid: {valid})"}



def _xaacp_required(**values: str) -> dict | None:
    missing = [name for name, value in values.items() if not str(value or "").strip()]
    if not missing:
        return None
    return {
        "ok": False,
        "status": "invalid",
        "error": "XAACP requires explicit " + ", ".join(missing),
        "missing": missing,
    }




def _xaacp_expire_due(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    message_id: str = "",
    session_id: str = "",
) -> int:
    """Apply TTL independently of inbox reads and return rows expired."""
    from . import xaacp_stream

    current = time.time() if now is None else float(now)
    where = (
        "WHERE direction='xaacp' AND status='pending' "
        "AND expires_at IS NOT NULL AND expires_at <= ?"
    )
    params: list[object] = [current]
    if message_id:
        where += " AND id=?"
        params.append(str(message_id).strip())
    if session_id:
        where += " AND session_id=?"
        params.append(str(session_id).strip())
    due = conn.execute(
        "SELECT id, session_id, lane_id, sender_actor_id, target_actor_id FROM messages "
        + where,
        params,
    ).fetchall()
    if not due:
        return 0
    expired = int(
        conn.execute(
            "UPDATE messages SET status='expired', decision_status='expired', "
            "answered_at=COALESCE(answered_at, ?) " + where,
            [current, *params],
        ).rowcount
        or 0
    )
    # #1061: expiry is an OUTCOME for the sender and voids a never-delivered
    # incoming event, in the same transaction as the status change.
    for row in due:
        xaacp_stream.supersede_undelivered(conn, message_id=str(row["id"]), now=current)
        sender = str(row["sender_actor_id"] or "")
        if sender and sender != str(row["target_actor_id"] or ""):
            xaacp_stream.append(
                conn,
                session_id=str(row["session_id"] or ""),
                reader_actor_id=sender,
                message_id=str(row["id"]),
                kind=xaacp_stream.EVENT_OUTCOME,
                lane_id=str(row["lane_id"] or ""),
                state="expired",
                now=current,
            )
    return expired
def _xaacp_row(row: sqlite3.Row) -> dict:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except (TypeError, ValueError):
        metadata = {}
    return {
        "id": row["id"],
        "protocol": row["protocol"] or _XAACP_PROTOCOL,
        "session_id": row["session_id"],
        "lane_id": row["lane_id"],
        "message_kind": row["message_kind"] or row["category"],
        "body": row["content"],
        "response": row["response"] or "",
        "status": str(row["decision_status"] or row["status"] or "pending"),
        "sender_actor_id": row["sender_actor_id"],
        "target_actor_id": row["target_actor_id"],
        "correlation_id": row["correlation_id"] or row["thread_id"] or row["id"],
        "reply_to_id": row["reply_to_id"],
        "metadata": metadata if isinstance(metadata, dict) else {},
        "wake_requested": bool(row["wake_requested"]),
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
        "expires_at": row["expires_at"],
        # DRT-12: projected BESIDE "status", never folded into it. A reader can
        # now tell "received, owner working" from silence while `status` is
        # still "pending" -- and the receipt survives the terminal decision.
        "acked_at": row["acked_at"],
        "acked_by": str(row["acked_by"] or ""),
        "ack_body": str(row["ack_body"] or ""),
    }


def _xaacp_addressed_responder_refusal(
    row: sqlite3.Row,
    responder_actor_id: str,
) -> dict | None:
    """THE addressed-responder check -- one home, no rival copy (DRT-12).

    `xaacp_reply` owned this comparison inline; `xaacp_ack` needs the identical
    rule ("only the actor the message is addressed TO may answer it"), and a
    second hand-written copy is how two surfaces drift into disagreeing about
    who a third party is. Lifted verbatim, so both callers refuse the same way.
    """
    if str(row["target_actor_id"] or "") != str(responder_actor_id or "").strip():
        return {
            "ok": False,
            "message_id": str(row["id"]),
            "status": "forbidden",
        }
    return None


#: The actor kinds that legitimately have NO lane, so a blank ``lane_id`` from
#: one of them is not a missing argument (#732, extended by #1007).
#:
#: "seat" and "agent" are the pre-#1007 vocabulary and stay for compatibility.
#: "conductor" and "subagent" are the names #1007 introduced and
#: ``xaacp_directory`` now EMITS: an actor that reports its own kind honestly
#: was being asked for a lane it does not have, which made every send and every
#: inbox read from a conductor or a subagent fail with "XAACP requires explicit
#: lane_id" — the unfollowable-remedy shape #732 exists to prevent, re-created
#: by a vocabulary change that did not reach this set. Measured 2026-09-04 by
#: the three-leg round-trip harness.
#:
#: "lane_worker" is DELIBERATELY ABSENT: a worker's route IS its lane, so a
#: blank lane_id from one is a real omission and must still refuse.
_XAACP_LANELESS_KINDS = frozenset({"seat", "agent", "conductor", "subagent"})


def _xaacp_spans_all_lanes(reader_actor_kind: str, lane_id: str) -> bool:
    """#1022: does this READ cover every lane addressed to the recipient?

    True only when the reader is a positively lane-less kind AND it named no
    lane. Naming a lane is an explicit request and still narrows; a lane worker
    (or an undeclared kind) never reaches here, because a blank lane_id from
    one is already refused as a missing field. Nothing about this predicate
    touches session_id or target_actor_id -- those stay exact everywhere.
    """
    if str(lane_id or "").strip():
        return False
    return str(reader_actor_kind or "").strip().lower() in _XAACP_LANELESS_KINDS


def _xaacp_authority_refusal(project_root: Path) -> dict | None:
    """#1059/#1033: ONE AUTHORITY SELECTION for every door into the local store.

    `xaacp_dispatch` already forwards every mode through `xaacp_authority_for`,
    but the store functions below are also reachable DIRECTLY (approval
    authority records its verdict via `xaacp_reply`). Without this check such a
    caller wrote a local decision while the project was cloud-bound -- the
    fork `remote_authority_for` exists to refuse. `None` means THIS store is
    the authority (unbound project, or gate-side execution); anything else
    refuses, with the unavailable authority's own reason, never a quiet
    `messages: []`.
    """
    authority = xaacp_authority_for(project_root)
    if authority is None:
        return None
    reason = str(getattr(authority, "reason", "") or "").strip()
    if reason:
        return {"ok": False, "status": "unavailable", "error": reason}
    return {
        "ok": False,
        "status": "not_authority",
        "error": (
            "this local XAACP store is not the authority for this cloud-bound "
            "project; call through ai_msg (xaacp_dispatch), which forwards to it"
        ),
    }


#: Read-ledger prefix for OUTCOME entries (#1059). Keyed on the SENDER and the
#: STATE, so an ack and the later decision are two announcements, and the
#: replier's own `xaacp:` reads can never hide the sender's answer.
_XAACP_OUTCOME_READ_PREFIX = "xaacp-outcome:"
#: wait_next's own once-ledger for outcomes (it has no rowid cursor to advance).
_XAACP_OUTCOME_WAIT_PREFIX = "xaacp-wait-outcome:"


def _xaacp_outcome_state(row: sqlite3.Row) -> str:
    """The sender-visible state of a message it sent, or "" if nothing to say.

    `canceled` is deliberately absent: only the SENDER can cancel, so
    announcing it back to that sender reports its own action as news.
    """
    status = str(row["decision_status"] or row["status"] or "pending")
    if status in _XAACP_TERMINAL:
        return "" if status == "canceled" else status
    return "acked" if row["acked_at"] is not None else ""


def _xaacp_entry(row: sqlite3.Row, *, reader_actor_id: str, kind: str, state: str = "") -> dict:
    entry = _xaacp_row(row)
    entry["entry_kind"] = kind
    entry["reader_actor_id"] = reader_actor_id
    entry["thread_id"] = entry["correlation_id"]
    if "seq" in set(row.keys()):
        # #1061: the event's position in the ONE ordered stream, assigned at
        # APPEND time. Delivery is its own fact, reported beside it.
        entry["cursor"] = int(row["seq"])
        entry["delivered_at"] = row["delivered_at"]
        entry["origin"] = str(row["origin"] or "agent")
        entry["causal_ref"] = str(row["causal_ref"] or "")
        if "claim_id" in set(row.keys()):
            entry["claim_id"] = str(row["claim_id"] or "")
            entry["claim_lease_expires_at"] = row["claim_lease_expires_at"]
            entry["reclaim_count"] = int(row["reclaim_count"] or 0)
    if kind == "outcome":
        entry["outcome_state"] = state
        entry["state_at"] = (
            row["acked_at"] if state == "acked" else (row["answered_at"] or row["acked_at"])
        ) or row["created_at"]
        entry["notice_id"] = f"{row['id']}:outcome:{state}"
    else:
        entry["state_at"] = row["created_at"]
        entry["notice_id"] = row["id"]
    return entry


def _xaacp_stream_entry(row: sqlite3.Row, *, reader_actor_id: str) -> dict:
    kind = str(row["event_kind"])
    return _xaacp_entry(
        row,
        reader_actor_id=reader_actor_id,
        kind=kind,
        state=str(row["event_state"] or "") if kind == "outcome" else "",
    )


def _xaacp_read_entries(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    actor_id: str,
    lane_id: str,
    span_all_lanes: bool,
    unread_only: bool,
    limit: int,
) -> tuple[list[dict], list[dict]]:
    """THE XAACP READ MODEL (#1059 -> #1061): (incoming, outcome) for one actor.

    Both lists are projections of the ONE ordered stream (`xaacp_stream`), in
    stream order -- the very events wait_next claims. NOTHING HERE WRITES: not
    delivery, not read. `unread_only` keeps its #1059 meaning: incoming still
    OUTSTANDING (the decision, not the read flag, decides) and outcomes in
    their CURRENT state not yet read by this actor; without it this is the
    full history. Identity axes stay exact: session exact, actor exact, lane
    as the inbox rule (#1022).
    """
    from . import xaacp_stream

    capped = max(1, min(int(limit), 500))
    rows = xaacp_stream.history(
        conn,
        session_id=session_id,
        actor_id=actor_id,
        lane_id=lane_id,
        span_all_lanes=span_all_lanes,
        limit=0,
    )
    incoming: list[dict] = []
    outcomes: list[dict] = []
    for row in rows:
        if row["event_kind"] == xaacp_stream.EVENT_INCOMING:
            status = str(row["decision_status"] or row["status"] or "pending")
            if unread_only and status in _XAACP_TERMINAL:
                continue
            incoming.append(_xaacp_stream_entry(row, reader_actor_id=actor_id))
            continue
        state = str(row["event_state"] or "")
        if unread_only:
            if state != _xaacp_outcome_state(row):
                continue  # overtaken by a later state of the same message
            seen = conn.execute(
                "SELECT 1 FROM msg_reads WHERE message_id=? AND role=?",
                (row["id"], f"{_XAACP_OUTCOME_READ_PREFIX}{actor_id}:{state}"),
            ).fetchone()
            if seen:
                continue
        outcomes.append(_xaacp_stream_entry(row, reader_actor_id=actor_id))
    if unread_only:
        return incoming[:capped], outcomes[:capped]
    return incoming[-capped:], outcomes[-capped:]


def xaacp_send(
    project_root: Path,
    *,
    session_id: str,
    sender_actor_id: str,
    target_actor_id: str,
    lane_id: str,
    message_kind: str,
    body: str,
    sender_actor_kind: str = "",
    correlation_id: str = "",
    reply_to_id: str = "",
    metadata: dict | None = None,
    wake: bool = False,
    ttl_seconds: float | None = None,
    target_route_state: str = "live",
    origin: str = "agent",
    causal_ref: str = "",
) -> dict:
    """Persist one actor-attributed XAACP message on an exact route.

    #1061: the message is appended to the ONE ordered stream for its target in
    the same transaction. ``target_route_state`` is what the dispatcher proved
    about the target's transport attachment; anything but ``live`` still
    QUEUES durably to the actor and reports ``delivery='deferred'`` -- a
    restart may drop a route, never the mailbox.
    """
    # #732: A SEAT HAS NO LANE. xaacp_directory rosters every seat with
    # lane_id="", so demanding one from EVERY sender made the remedy that the
    # seat refusal NAMES unfollowable -- it names no lane_id, and the caller it
    # names it to cannot supply one. The lane belongs to a WORKER, whose route
    # IS the lane; xaacp_inbox already conditions its lane check the same way,
    # and #640 applied the identical reasoning to the TARGET resolver. Only the
    # sender-side validation was left behind.
    #
    # UNKNOWN STAYS REQUIRED. The skip applies only to a kind positively known
    # to be lane-less, so a caller that forgets to declare its kind keeps the
    # old stricter behaviour instead of silently loosening it.
    required = {
        "session_id": session_id,
        "sender_actor_id": sender_actor_id,
        "target_actor_id": target_actor_id,
        "message_kind": message_kind,
        "body": body,
    }
    if str(sender_actor_kind or "").strip().lower() not in _XAACP_LANELESS_KINDS:
        required["lane_id"] = lane_id
    invalid = _xaacp_required(**required)
    if invalid:
        return invalid
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return refused
    session_id = str(session_id).strip()
    sender_actor_id = str(sender_actor_id).strip()
    target_actor_id = str(target_actor_id).strip()
    lane_id = str(lane_id).strip()
    message_kind = str(message_kind).strip()
    reply_to_id = str(reply_to_id or "").strip()
    requested_correlation = str(correlation_id or "").strip()
    now = time.time()
    message_id = str(uuid4())[:12]

    expires_at: float | None = None
    if ttl_seconds is not None:
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError):
            return {
                "ok": False,
                "status": "invalid",
                "error": "ttl_seconds must be a finite non-negative number",
            }
        if ttl < 0 or ttl == float("inf") or ttl == float("-inf") or ttl != ttl:
            return {
                "ok": False,
                "status": "invalid",
                "error": "ttl_seconds must be a finite non-negative number",
            }
        expires_at = now + ttl

    with _connect(project_root) as conn:
        correlation = requested_correlation or message_id
        if reply_to_id:
            parent = conn.execute(
                "SELECT id, session_id, lane_id, sender_actor_id, target_actor_id, "
                "correlation_id, thread_id FROM messages "
                "WHERE id=? AND direction='xaacp'",
                (reply_to_id,),
            ).fetchone()
            if parent is None or str(parent["session_id"] or "") != session_id:
                return {
                    "ok": False,
                    "status": "not_found",
                    "message_id": reply_to_id,
                }
            if str(parent["lane_id"] or "") != lane_id:
                return {
                    "ok": False,
                    "status": "forbidden",
                    "error": "XAACP replies cannot cross lane routes",
                    "message_id": reply_to_id,
                }
            parent_sender = str(parent["sender_actor_id"] or "").strip()
            parent_target = str(parent["target_actor_id"] or "").strip()
            if (
                sender_actor_id not in {parent_sender, parent_target}
                or target_actor_id not in {parent_sender, parent_target}
                or sender_actor_id == target_actor_id
            ):
                return {
                    "ok": False,
                    "status": "forbidden",
                    "error": "XAACP reply actors must be the parent participants",
                    "message_id": reply_to_id,
                }
            parent_correlation = str(
                parent["correlation_id"] or parent["thread_id"] or parent["id"]
            ).strip()
            if requested_correlation and requested_correlation != parent_correlation:
                return {
                    "ok": False,
                    "status": "invalid",
                    "error": "correlation_id cannot fork an in_reply_to thread",
                    "message_id": reply_to_id,
                    "correlation_id": parent_correlation,
                }
            correlation = parent_correlation

        conn.execute(
            "INSERT INTO messages "
            "(id, lane_id, session_id, direction, category, content, status, "
            "created_at, protocol, message_kind, sender_actor_id, target_actor_id, "
            "correlation_id, reply_to_id, metadata_json, wake_requested, expires_at, "
            "thread_id) VALUES (?, ?, ?, 'xaacp', ?, ?, 'pending', ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                message_id,
                lane_id,
                session_id,
                message_kind,
                str(body),
                now,
                _XAACP_PROTOCOL,
                message_kind,
                sender_actor_id,
                target_actor_id,
                correlation,
                reply_to_id,
                json.dumps(dict(metadata or {}), sort_keys=True, default=str),
                1 if wake else 0,
                expires_at,
                correlation,
            ),
        )
        from . import xaacp_stream

        stream_cursor = xaacp_stream.append(
            conn,
            session_id=session_id,
            reader_actor_id=target_actor_id,
            message_id=message_id,
            kind=xaacp_stream.EVENT_INCOMING,
            lane_id=lane_id,
            now=now,
            # SERVER-SET provenance: the dispatcher never forwards a caller
            # value here; an operator surface calls xaacp_send directly.
            origin=origin,
            causal_ref=causal_ref,
        )
        conn.commit()
    route_state = str(target_route_state or "live").strip() or "live"
    delivery = "immediate" if route_state == "live" else "deferred"

    wake_queued = False
    mailbox_id = 0
    if wake:
        try:
            from .lane_mailbox_store import LaneMailboxStore
            from .session_lane_agents_store import SessionLaneAgentsStore

            exact_worker = next(
                (
                    worker
                    for worker in SessionLaneAgentsStore().get_lane_agents(
                        project_root, session_id
                    )
                    if str(worker.get("worker_id") or "") == target_actor_id
                    and str(worker.get("lane_id") or "") == lane_id
                ),
                None,
            )
            if exact_worker is not None:
                mailbox_id = LaneMailboxStore().put(
                    project_root,
                    worker_id=target_actor_id,
                    session_id=session_id,
                    prompt=str(body),
                    author_session_id=session_id,
                    protocol=_XAACP_PROTOCOL,
                    message_id=message_id,
                    correlation_id=correlation,
                    sender_actor_id=sender_actor_id,
                    target_actor_id=target_actor_id,
                    lane_id=lane_id,
                    message_kind=message_kind,
                )
                wake_queued = bool(mailbox_id)
        except Exception:
            logger.exception("XAACP wake projection failed for message %s", message_id)

    audit_payload = {
        "protocol": _XAACP_PROTOCOL,
        "message_id": message_id,
        "session_id": session_id,
        "lane_id": lane_id,
        "message_kind": message_kind,
        "sender_actor_id": sender_actor_id,
        "target_actor_id": target_actor_id,
        "correlation_id": correlation,
        "reply_to_id": reply_to_id,
        "wake_requested": bool(wake),
        "wake_queued": wake_queued,
        "mailbox_id": mailbox_id,
        "expires_at": expires_at,
        "stream_cursor": stream_cursor,
        "target_route_state": route_state,
        "delivery": delivery,
    }
    _audit_event(
        project_root,
        session_id,
        action_kind="xaacp_send",
        target_entity=target_actor_id,
        payload=audit_payload,
        event_kind="stream_send",
    )
    result = {
        "ok": True,
        "message_id": message_id,
        "correlation_id": correlation,
        "status": "pending",
        "wake_queued": wake_queued,
        "mailbox_id": mailbox_id,
        "expires_at": expires_at,
        "stream_cursor": stream_cursor,
        "delivery": delivery,
        "target_route_state": route_state,
    }
    if delivery == "deferred":
        result["delivery_note"] = (
            "queued to the durable actor mailbox; the target's route is "
            f"{route_state!r}, so it is delivered on that actor's next read "
            "(wait_next / inbox / next tool-result notice)"
        )
    return result


def xaacp_inbox(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    lane_id: str,
    unread_only: bool = True,
    mark_read: bool = False,
    reader_actor_kind: str = "",
    limit: int = 50,
) -> dict:
    """Read one exact XAACP route; never broaden across any identity axis."""
    # #732, READ side: the same rule as the send path. A SEAT has no lane, so
    # requiring one to read its OWN mailbox locked the seat out of the very
    # messages the remedy told it to exchange -- the send was accepted and then
    # unreadable, which is a worse shape than refusing the send outright.
    # Unknown kind stays strict; the route's lane scoping for WORKERS is
    # enforced by the caller-vs-requested lane check in the dispatcher and is
    # untouched here.
    required = {
        "session_id": session_id,
        "target_actor_id": target_actor_id,
    }
    if str(reader_actor_kind or "").strip().lower() not in _XAACP_LANELESS_KINDS:
        required["lane_id"] = lane_id
    invalid = _xaacp_required(**required)
    if invalid:
        return {**invalid, "messages": []}
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        # AN INBOX THAT CANNOT BE READ IS NOT AN EMPTY INBOX: no `messages` key.
        return refused
    session_id = str(session_id).strip()
    target_actor_id = str(target_actor_id).strip()
    lane_id = str(lane_id).strip()
    # #1022: a lane-less reader naming no lane spans every lane addressed to
    # it; session_id and target_actor_id stay EXACT either way.
    span_all_lanes = _xaacp_spans_all_lanes(reader_actor_kind, lane_id)
    reader_key = "xaacp:" + target_actor_id
    now = time.time()
    with _connect(project_root) as conn:
        _xaacp_expire_due(conn, now=now, session_id=session_id)
        # ONE READ MODEL (#1059). `unread_only` means OUTSTANDING for incoming
        # (the decision, not the read flag, decides visibility) and
        # NOT-YET-READ-IN-THIS-STATE for outcomes of messages I sent.
        incoming, outcomes = _xaacp_read_entries(
            conn,
            session_id=session_id,
            actor_id=target_actor_id,
            lane_id=lane_id,
            span_all_lanes=span_all_lanes,
            unread_only=unread_only,
            limit=limit,
        )
        if mark_read and (incoming or outcomes):
            conn.executemany(
                "INSERT OR IGNORE INTO msg_reads (message_id, role, read_at) "
                "VALUES (?, ?, ?)",
                [(m["id"], reader_key, now) for m in incoming]
                + [
                    (
                        m["id"],
                        f"{_XAACP_OUTCOME_READ_PREFIX}{target_actor_id}:{m['outcome_state']}",
                        now,
                    )
                    for m in outcomes
                ],
            )
        conn.commit()
    # #1061: ONE ORDER -- the stream's, across sends, replies and outcomes.
    messages = sorted(incoming + outcomes, key=lambda item: int(item.get("cursor") or 0))
    if mark_read and messages:
        _audit_event(
            project_root,
            session_id,
            action_kind="xaacp_read",
            target_entity=target_actor_id,
            payload={
                "session_id": session_id,
                "lane_id": lane_id,
                "target_actor_id": target_actor_id,
                "message_ids": [item["id"] for item in messages],
                "stream_cursors": [item.get("cursor") for item in messages],
            },
            event_kind="stream_read",
        )
    return {"ok": True, "messages": messages}


def _xaacp_park_record(
    project_root: Path, session_id: str, actor_id: str, *, enter: bool, return_kind: str = ""
) -> None:
    """Best-effort park bookkeeping; never alters a delivery."""
    try:
        now = time.time()
        with _connect(project_root) as conn:
            if enter:
                conn.execute(
                    "INSERT INTO xaacp_park_log (session_id, actor_id, last_park_enter_at, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, actor_id) DO UPDATE SET "
                    "last_park_enter_at=excluded.last_park_enter_at, updated_at=excluded.updated_at",
                    (session_id, actor_id, now, now),
                )
            else:
                conn.execute(
                    "UPDATE xaacp_park_log SET last_park_return_at=?, last_return_kind=?, updated_at=? "
                    "WHERE session_id=? AND actor_id=?",
                    (now, return_kind, now, session_id, actor_id),
                )
            conn.commit()
    except Exception:
        logger.debug("xaacp park bookkeeping failed", exc_info=True)


#: Upper bound on events one wait_next return hands over (operator ruling:
#: "all pending ones, in order, bounded").
XAACP_WAIT_NEXT_BATCH = 20


def xaacp_stream_activity_marker(
    project_root: Path,
    *,
    session_id: str,
    actor_id: str,
) -> dict:
    """THE CAUSAL ACTIVITY MARKER (#1061, for lane-consul-stop). Read-only.

    `marker` changes iff the actor's stream saw activity: an event addressed to
    it (incoming or outcome) or an event created by a message it sent. An idle
    wait_next timeout leaves it unchanged, so a Stop that re-parks can compare
    markers and emit NO duplicate turn mirror. Session-scoped, exact actor.
    """
    sid = str(session_id or "").strip()
    aid = str(actor_id or "").strip()
    if not sid or not aid:
        return {"ok": False, "status": "invalid", "error": "session_id and actor_id required"}
    with _connect(project_root) as conn:
        received = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM xaacp_stream WHERE session_id=? AND reader_actor_id=?",
            (sid, aid),
        ).fetchone()[0]
        sent = conn.execute(
            "SELECT COALESCE(MAX(s.seq), 0) FROM xaacp_stream s JOIN messages m ON m.id = s.message_id "
            "WHERE s.session_id=? AND m.sender_actor_id=? AND s.event_kind='incoming'",
            (sid, aid),
        ).fetchone()[0]
    received, sent = int(received or 0), int(sent or 0)
    return {
        "ok": True,
        "session_id": sid,
        "actor_id": aid,
        "last_received_cursor": received,
        "last_sent_cursor": sent,
        "marker": f"{sid}:{aid}:{max(received, sent)}",
    }


def xaacp_wait_next(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    lane_id: str,
    reader_actor_kind: str = "",
    after_cursor: int = 0,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.25,
) -> dict:
    """Block for the next event on the caller's ONE ordered stream (#1061).

    Returns the oldest undelivered stream event (incoming message or outcome of
    a message the caller sent) with ``cursor > max(durable_receive_cursor,
    after_cursor)``, claiming its delivery atomically. ``after_cursor`` is only a
    lower bound: it never rewinds, and an already-delivered event is never
    returned again. History and replay belong to ``xaacp_inbox``. Returning an
    event does NOT mark it read or decide it.
    """
    required = {
        "session_id": session_id,
        "target_actor_id": target_actor_id,
    }
    if str(reader_actor_kind or "").strip().lower() not in _XAACP_LANELESS_KINDS:
        required["lane_id"] = lane_id
    invalid = _xaacp_required(**required)
    if invalid:
        return {**invalid, "cursor": after_cursor, "message": None}
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return {**refused, "cursor": after_cursor}

    session_id = str(session_id).strip()
    target_actor_id = str(target_actor_id).strip()
    lane_id = str(lane_id).strip()
    # #1022: a blocking wait must see exactly what the polled inbox sees --
    # otherwise a conductor parked on wait_next sleeps through the very lane
    # report it is waiting for. Same predicate, same rule, same exact
    # session_id/target_actor_id on both sides.
    span_all_lanes = _xaacp_spans_all_lanes(reader_actor_kind, lane_id)
    try:
        cursor = int(after_cursor)
        timeout = float(timeout_seconds)
        poll_interval = float(poll_interval_seconds)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "status": "invalid",
            "error": "after_cursor must be an integer and timeout values must be finite numbers",
            "cursor": after_cursor,
            "message": None,
        }
    if cursor < 0 or timeout != timeout or poll_interval != poll_interval or timeout in {float("inf"), float("-inf")} or poll_interval in {float("inf"), float("-inf")}:
        return {
            "ok": False,
            "status": "invalid",
            "error": "after_cursor must be non-negative and timeout values must be finite numbers",
            "cursor": cursor,
            "message": None,
        }

    # ── DRT-11/13: THE PARK IS THE LEASE'S ARM AND ITS SETTLE ────────────
    # `ReceiveLeaseStore.settle` HAD NO PRODUCTION CALLER. Every lease armed
    # at Stop therefore aged into `rearm_overdue` EVEN WHEN THE ACTOR PARKED
    # CORRECTLY, which makes the first thing the observability surface ships a
    # FALSE-ALARM GENERATOR: a dashboard reporting every actor delinquent,
    # including the ones doing exactly the right thing. That had to be closed
    # before anything consumes `overdue` / `claim_alarm`, because an alarm
    # surface that cries wolf on its first day is one an operator learns to
    # ignore permanently.
    #
    # ARM BEFORE THE PARK CAN BLOCK, SETTLE AT EVERY RETURN. The arm goes
    # here -- after validation, before the backlog probe -- rather than next
    # to `deadline` below, because the backlog probe can RETURN a message
    # without ever reaching `deadline`; arming later would leave that whole
    # path unaccounted. All three exits (backlog, message, timeout) settle.
    #
    # ENTIRELY INSIDE try/except: this is bookkeeping wrapped around a
    # delivery rail. A broken lease ledger must never be able to fail, delay
    # or alter a message delivery -- the lease describes the park, it does
    # not govern it.
    _lease_route = {
        "session_id": session_id,
        "actor_id": target_actor_id,
        "lane_id": lane_id,
        "actor_kind": str(reader_actor_kind or ""),
    }
    try:
        from .receive_lease_store import ReceiveLeaseStore as _ReceiveLeaseStore

        _ReceiveLeaseStore().arm(
            project_root,
            lease_seconds=max(float(timeout_seconds or 0.0), 1.0) + 30.0,
            cursor=cursor,
            **_lease_route,
        )
    except Exception:
        pass
    # #1061 consul-stop: the AUTHORITY's own park bookkeeping (this function
    # runs gate-side for cloud actors), readable via xaacp_last_park.
    _xaacp_park_record(project_root, session_id, target_actor_id, enter=True)
    _audit_event(
        project_root,
        session_id,
        action_kind="wait_next_park_enter",
        target_entity=target_actor_id,
        payload={
            "session_id": session_id,
            "actor_id": target_actor_id,
            "lane_id": lane_id,
            "actor_kind": str(reader_actor_kind or ""),
            "after_cursor": cursor,
            "timeout_seconds": timeout,
        },
        event_kind="consul_park_enter",
    )

    def _lease_settle(return_kind: str, delivered: int) -> None:
        _xaacp_park_record(
            project_root, session_id, target_actor_id, enter=False, return_kind=return_kind
        )
        try:
            from .receive_lease_store import ReceiveLeaseStore as _RLS

            _RLS().settle(
                project_root,
                session_id=_lease_route["session_id"],
                actor_id=_lease_route["actor_id"],
                lane_id=_lease_route["lane_id"],
                return_kind=return_kind,
                cursor=int(delivered),
            )
        except Exception:
            pass

    # #1061 THE RECEIVE CURSOR CONTRACT (r0b, 2026-09-14). The stream event's
    # `seq` is assigned at APPEND time; a return CLAIMS delivery of exactly one
    # event with seq > max(server-held durable_receive_cursor, after_cursor),
    # compare-and-set on `delivered_at IS NULL`, so it can never be handed out
    # twice -- not to this waiter, not to a concurrent notice rail. Measured
    # defects this replaces: (1) after_cursor=0 bootstrapped on the oldest
    # OUTSTANDING row, and an ACKED-but-undecided row is outstanding, so it
    # replayed; (2) cursor>0 then walked every later ROWID regardless of state,
    # replaying hours-old decided rows in order (45 -> 48 -> 49 -> 52 -> 53);
    # (3) outcome entries had no position of their own and were returned with
    # the CALLER'S cursor echoed back, so two different events both came back
    # "at cursor 55". Delivery, read and decision stay three facts: a claim
    # never marks read and never decides.
    from . import xaacp_stream

    deadline = time.monotonic() + max(0.0, min(timeout, 300.0))
    poll_interval = max(0.01, min(poll_interval, 1.0))
    while True:
        now = time.time()
        with _connect(project_root) as conn:
            _xaacp_expire_due(conn, now=now, session_id=session_id)
            # OPERATOR RULING 2026-09-14: wait_next is a blocking read that
            # hands the reader ALL its pending undelivered events, in order,
            # bounded -- then unblocks. Each is its own CAS claim.
            events = []
            floor = cursor
            while len(events) < XAACP_WAIT_NEXT_BATCH:
                claimed = xaacp_stream.claim_next(
                    conn,
                    session_id=session_id,
                    actor_id=target_actor_id,
                    lane_id=lane_id,
                    span_all_lanes=span_all_lanes,
                    after_cursor=floor,
                    via="wait_next",
                    now=now,
                )
                if claimed is None:
                    break
                events.append(claimed)
                floor = int(claimed["seq"])
            event = events[0] if events else None
            durable = xaacp_stream.receive_cursor(
                conn,
                session_id=session_id,
                actor_id=target_actor_id,
                lane_id=lane_id,
                span_all_lanes=span_all_lanes,
                now=now,
            )
            conn.commit()
        if event is not None:
            entries = [_xaacp_stream_entry(e, reader_actor_id=target_actor_id) for e in events]
            delivered_cursor = int(events[-1]["seq"])
            _audit_event(
                project_root,
                session_id,
                action_kind="wait_next_deliver",
                target_entity=target_actor_id,
                payload={
                    "session_id": session_id,
                    "lane_id": lane_id,
                    "reader_actor_id": target_actor_id,
                    "deliveries": [
                        {
                            "message_id": item["id"],
                            "entry_kind": item["entry_kind"],
                            "outcome_state": item.get("outcome_state", ""),
                            "stream_cursor": item.get("cursor"),
                            "origin": item.get("origin", ""),
                        }
                        for item in entries
                    ],
                    "durable_receive_cursor": durable,
                    "surface": "wait_next",
                },
                event_kind="stream_deliver",
            )
            # SETTLE SITE 1 of 2 -- a delivery (backlog or wake) ended the park.
            _lease_settle("message", delivered_cursor)
            return {
                "ok": True,
                "status": "message",
                "cursor": delivered_cursor,
                "durable_receive_cursor": durable,
                # `message` stays the FIRST event for pre-#1061 callers;
                # `messages` is every event this call delivered, in order.
                "message": entries[0],
                "messages": entries,
            }
        if time.monotonic() >= deadline:
            # SETTLE SITE 2 of 2 -- the park expired with nothing delivered.
            # A timeout does NOT end the receive obligation (RETURN_KINDS'
            # whole point): the actor is still active, so it still owes
            # another arm. `settle` moves it to `working`, which is what
            # makes a never-re-arming actor become `rearm_overdue` instead of
            # sitting in `waiting` looking permanently healthy.
            resume = max(cursor, durable)
            _lease_settle("timeout", resume)
            return {
                "ok": True,
                "status": "timeout",
                "cursor": resume,
                "durable_receive_cursor": durable,
                "message": None,
            }
        time.sleep(poll_interval)


def _xaacp_stream_record_action(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    actor_id: str,
    state: str,
    now: float,
) -> None:
    """#1061: the addressee acted (ack / decision) -- one home for both doors.

    Two stream facts, same transaction as the row update: the addressee's
    incoming event is DELIVERED (it cannot act on what it never received --
    delivery only, never read), and the sender gets an OUTCOME event appended
    at its true position in the ordered stream.
    """
    from . import xaacp_stream

    message_id = str(row["id"])
    xaacp_stream.mark_delivered_by_action(
        conn, message_id=message_id, reader_actor_id=actor_id, via=f"action:{state}", now=now
    )
    sender = str(row["sender_actor_id"] or "")
    if sender and sender != str(row["target_actor_id"] or ""):
        xaacp_stream.append(
            conn,
            session_id=str(row["session_id"] or ""),
            reader_actor_id=sender,
            message_id=message_id,
            kind=xaacp_stream.EVENT_OUTCOME,
            lane_id=str(row["lane_id"] or ""),
            state=state,
            now=now,
        )


def xaacp_reply(
    project_root: Path,
    *,
    message_id: str,
    session_id: str,
    responder_actor_id: str,
    decision: str,
    body: str = "",
) -> dict:
    invalid = _xaacp_required(
        message_id=message_id,
        session_id=session_id,
        responder_actor_id=responder_actor_id,
        decision=decision,
    )
    if invalid:
        return invalid
    message_id = str(message_id).strip()
    session_id = str(session_id).strip()
    responder_actor_id = str(responder_actor_id).strip()
    decision = str(decision).strip().lower()
    if decision not in _XAACP_DECISIONS:
        return {
            "ok": False,
            "status": "invalid",
            "error": f"decision must be one of {sorted(_XAACP_DECISIONS)}",
        }
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return {**refused, "message_id": message_id}
    with _connect(project_root) as conn:
        _xaacp_expire_due(
            conn,
            message_id=message_id,
            session_id=session_id,
        )
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? "
            "AND direction='xaacp'",
            (message_id, session_id),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "not_found",
            }
        forbidden = _xaacp_addressed_responder_refusal(row, responder_actor_id)
        if forbidden is not None:
            return forbidden
        current = str(row["decision_status"] or row["status"] or "pending")
        if current in _XAACP_TERMINAL:
            conn.commit()
            return {
                "ok": True,
                "message_id": message_id,
                "status": current,
                "response": str(row["response"] or ""),
            }
        answered_at = time.time()
        updated = conn.execute(
            "UPDATE messages SET response=?, status=?, decision_status=?, "
            "answered_at=? WHERE id=? AND session_id=? AND direction='xaacp' "
            "AND status='pending' AND COALESCE(decision_status, '') IN ('', 'pending')",
            (str(body), decision, decision, answered_at, message_id, session_id),
        ).rowcount
        if not updated:
            raced = conn.execute(
                "SELECT status, decision_status, response FROM messages "
                "WHERE id=? AND session_id=? AND direction='xaacp'",
                (message_id, session_id),
            ).fetchone()
            conn.commit()
            if raced is None:
                return {
                    "ok": False,
                    "message_id": message_id,
                    "status": "not_found",
                }
            return {
                "ok": True,
                "message_id": message_id,
                "status": str(
                    raced["decision_status"] or raced["status"] or "pending"
                ),
                "response": str(raced["response"] or ""),
            }
        _xaacp_stream_record_action(
            conn, row, actor_id=responder_actor_id, state=decision, now=answered_at
        )
        conn.commit()

    _audit_event(
        project_root,
        session_id,
        event_kind="stream_decide",
        action_kind="xaacp_reply",
        target_entity=str(row["sender_actor_id"]),
        payload={
            "message_id": message_id,
            "session_id": session_id,
            "lane_id": str(row["lane_id"]),
            "sender_actor_id": str(row["sender_actor_id"]),
            "target_actor_id": str(row["target_actor_id"]),
            "responder_actor_id": responder_actor_id,
            "correlation_id": str(row["correlation_id"]),
            "decision": decision,
        },
    )
    return {
        "ok": True,
        "message_id": message_id,
        "status": decision,
        "response": str(body),
    }


def xaacp_ack(
    project_root: Path,
    *,
    message_id: str,
    session_id: str,
    body: str = "",
) -> dict:
    """Acknowledge RECEIPT of one message WITHOUT concluding it (DRT-12).

    The defect this closes: every `xaacp_reply` decision is in
    `_XAACP_TERMINAL`, so `accepted` could never mean "I have this and I own the
    response" -- the same message could then never reach `completed` or
    `blocked`. The fast-ACK meaning was unusable, and receipt, responsibility
    and outcome were one column. This writes the MIDDLE fact and nothing else:
    `status` / `decision_status` / `answered_at` / `response` are untouched, so
    the terminal decision still lands on the same row afterwards.

    IDENTITY IS DERIVED, NEVER CLAIMED. There is deliberately no parameter for
    the acker -- it comes from `xaacp_resolve_caller_route`, the same resolver
    `ai_msg` uses for sender attribution, and an unresolvable caller is NOBODY
    (empire: unknown is not pass). `xaacp_reply` still accepts a
    `responder_actor_id` because its only caller is the dispatcher that derived
    it; this surface is reachable directly, so it takes the stricter shape of
    `approval_authority.decide_approval`.

    ACK FIRES ONCE (operator ruling 2026-09-10: notifications fire once so
    agents do not get confused). The fire-once guard is the `acked_at IS NULL`
    clause ON the UPDATE, not a read-then-write -- two actors racing a SELECT
    would both see "not acked" and both announce. A second ACK returns the
    STANDING receipt with `ack_announced=False`, announces nothing, and writes
    no second audit row.

    ACK AFTER A TERMINAL DECISION IS REFUSED, not absorbed. The work already
    concluded; acknowledging receipt afterwards means nothing, and accepting it
    would let an actor launder a late ACK into apparent responsiveness.

    ACK DOES NOT END THE RECEIVE OBLIGATION (DRT-13). Nothing here tears down a
    wait, clears a cursor or retires an actor: an active actor's duty to re-arm
    `wait_next` survives ACK exactly as it survives a response or a timeout.
    """
    invalid = _xaacp_required(message_id=message_id, session_id=session_id)
    if invalid:
        return invalid
    message_id = str(message_id).strip()
    session_id = str(session_id).strip()
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return {**refused, "message_id": message_id}

    try:
        route = xaacp_resolve_caller_route(project_root)
    except Exception:  # pragma: no cover - the resolver is defensive already
        logger.exception("XAACP ack caller resolution failed")
        route = {}
    acked_by = str(route.get("actor_id") or "").strip()
    if not acked_by or acked_by == MSG_ROLE_UNMAPPED:
        return {
            "ok": False,
            "message_id": message_id,
            "status": "unidentified",
            "error": (
                "the acknowledging actor could not be derived; an ACK from "
                "nobody is not an ACK"
            ),
        }

    with _connect(project_root) as conn:
        _xaacp_expire_due(
            conn,
            message_id=message_id,
            session_id=session_id,
        )
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? "
            "AND direction='xaacp'",
            (message_id, session_id),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "not_found",
            }
        # ONE addressed-responder rule, shared with xaacp_reply.
        forbidden = _xaacp_addressed_responder_refusal(row, acked_by)
        if forbidden is not None:
            conn.commit()
            return forbidden
        current = str(row["decision_status"] or row["status"] or "pending")
        if current in _XAACP_TERMINAL:
            conn.commit()
            return {
                "ok": False,
                "message_id": message_id,
                "status": "already_terminal",
                "error": (
                    "this message already reached a terminal decision; "
                    "acknowledging receipt after the fact is meaningless"
                ),
                "decision_status": current,
                "acked_at": row["acked_at"],
                "acked_by": str(row["acked_by"] or ""),
                "ack_body": str(row["ack_body"] or ""),
            }
        acked_at = time.time()
        updated = conn.execute(
            "UPDATE messages SET acked_at=?, acked_by=?, ack_body=? "
            "WHERE id=? AND session_id=? AND direction='xaacp' "
            "AND acked_at IS NULL",
            (acked_at, acked_by, str(body), message_id, session_id),
        ).rowcount
        if updated:
            _xaacp_stream_record_action(
                conn, row, actor_id=acked_by, state=XAACP_ACK_STATUS, now=acked_at
            )
        standing = conn.execute(
            "SELECT acked_at, acked_by, ack_body, status, decision_status "
            "FROM messages WHERE id=? AND session_id=? AND direction='xaacp'",
            (message_id, session_id),
        ).fetchone()
        conn.commit()

    if standing is None:
        # The row vanished between the read and the write. Fail closed rather
        # than report an ACK against a message that no longer exists.
        return {
            "ok": False,
            "message_id": message_id,
            "status": "not_found",
        }
    result = {
        "ok": True,
        "message_id": message_id,
        "status": XAACP_ACK_STATUS,
        "acked_at": standing["acked_at"],
        "acked_by": str(standing["acked_by"] or ""),
        "ack_body": str(standing["ack_body"] or ""),
        # THE WORK STATUS, REPORTED UNCHANGED. Carrying it back is what makes
        # the separation legible at the call site: an ACK that silently moved
        # this would be the very conflation DRT-12 is about.
        "decision_status": str(
            standing["decision_status"] or standing["status"] or "pending"
        ),
        "ack_announced": bool(updated),
    }
    if not updated:
        # A SECOND ACK IS NOT A SECOND EVENT: standing receipt, no announcement,
        # no audit row. `must_act` workflow traffic fires once or agents act on
        # the same obligation twice.
        result["existing"] = True
        return result

    _audit_event(
        project_root,
        session_id,
        event_kind="stream_ack",
        action_kind="xaacp_ack",
        target_entity=str(row["sender_actor_id"]),
        payload={
            "message_id": message_id,
            "session_id": session_id,
            "lane_id": str(row["lane_id"]),
            "sender_actor_id": str(row["sender_actor_id"]),
            "target_actor_id": str(row["target_actor_id"]),
            "acked_by": acked_by,
            "correlation_id": str(row["correlation_id"]),
            "acked_at": acked_at,
            # Proof in the audit trail that the ACK did NOT decide anything.
            "decision_status": result["decision_status"],
        },
    )
    return result


def xaacp_wait(
    project_root: Path,
    *,
    message_id: str,
    session_id: str,
    actor_id: str,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.05,
) -> dict:
    """Poll a decision without conflating timeout with transport failure."""
    invalid = _xaacp_required(
        message_id=message_id,
        session_id=session_id,
        actor_id=actor_id,
    )
    if invalid:
        return invalid
    message_id = str(message_id).strip()
    session_id = str(session_id).strip()
    actor_id = str(actor_id).strip()
    try:
        timeout = float(timeout_seconds)
        poll_interval = float(poll_interval_seconds)
    except (TypeError, ValueError):
        return {
            "ok": False,
            "message_id": message_id,
            "status": "invalid",
            "error": "timeout and poll interval must be finite numbers",
        }
    nonfinite = {float("inf"), float("-inf")}
    if (
        timeout != timeout
        or poll_interval != poll_interval
        or timeout in nonfinite
        or poll_interval in nonfinite
    ):
        return {
            "ok": False,
            "message_id": message_id,
            "status": "invalid",
            "error": "timeout and poll interval must be finite numbers",
        }
    deadline = time.monotonic() + max(0.0, min(timeout, 30.0))
    poll_interval = max(0.01, min(poll_interval, 0.25))
    while True:
        with _connect(project_root) as conn:
            _xaacp_expire_due(
                conn,
                message_id=message_id,
                session_id=session_id,
            )
            row = conn.execute(
                "SELECT * FROM messages WHERE id=? AND session_id=? "
                "AND direction='xaacp'",
                (message_id, session_id),
            ).fetchone()
            conn.commit()
        if row is None:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "not_found",
            }
        if actor_id not in {
            str(row["sender_actor_id"] or ""),
            str(row["target_actor_id"] or ""),
        }:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "forbidden",
            }
        # DRT-12: the receipt travels with BOTH answers. An ACK is not a
        # decision, so it must not satisfy this wait -- but a sender that times
        # out needs to tell "received, owner working" from silence, which is the
        # whole reason the fact exists. So it rides alongside, never instead.
        ack_facts = {
            "acked_at": row["acked_at"],
            "acked_by": str(row["acked_by"] or ""),
            "ack_body": str(row["ack_body"] or ""),
        }
        status = str(row["decision_status"] or row["status"] or "pending")
        if status in _XAACP_TERMINAL:
            return {
                "ok": True,
                "message_id": message_id,
                "status": status,
                "response": str(row["response"] or ""),
                **ack_facts,
            }
        if time.monotonic() >= deadline:
            return {
                "ok": True,
                "message_id": message_id,
                "status": "timeout",
                "response": "",
                **ack_facts,
            }
        time.sleep(poll_interval)


def xaacp_cancel(
    project_root: Path,
    *,
    message_id: str,
    session_id: str,
    sender_actor_id: str,
    reason: str = "",
) -> dict:
    invalid = _xaacp_required(
        message_id=message_id,
        session_id=session_id,
        sender_actor_id=sender_actor_id,
    )
    if invalid:
        return invalid
    message_id = str(message_id).strip()
    session_id = str(session_id).strip()
    sender_actor_id = str(sender_actor_id).strip()
    refused = _xaacp_authority_refusal(project_root)
    if refused is not None:
        return {**refused, "message_id": message_id}
    with _connect(project_root) as conn:
        _xaacp_expire_due(
            conn,
            message_id=message_id,
            session_id=session_id,
        )
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? "
            "AND direction='xaacp'",
            (message_id, session_id),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "not_found",
            }
        if str(row["sender_actor_id"] or "") != sender_actor_id:
            return {
                "ok": False,
                "message_id": message_id,
                "status": "forbidden",
            }
        current = str(row["decision_status"] or row["status"] or "pending")
        if current in _XAACP_TERMINAL:
            conn.commit()
            return {
                "ok": True,
                "message_id": message_id,
                "status": current,
            }
        answered_at = time.time()
        updated = conn.execute(
            "UPDATE messages SET response=?, status='canceled', "
            "decision_status='canceled', answered_at=? "
            "WHERE id=? AND session_id=? AND direction='xaacp' "
            "AND status='pending' AND COALESCE(decision_status, '') IN ('', 'pending')",
            (str(reason), answered_at, message_id, session_id),
        ).rowcount
        if not updated:
            raced = conn.execute(
                "SELECT status, decision_status FROM messages "
                "WHERE id=? AND session_id=? AND direction='xaacp'",
                (message_id, session_id),
            ).fetchone()
            conn.commit()
            if raced is None:
                return {
                    "ok": False,
                    "message_id": message_id,
                    "status": "not_found",
                }
            return {
                "ok": True,
                "message_id": message_id,
                "status": str(
                    raced["decision_status"] or raced["status"] or "pending"
                ),
            }
        from . import xaacp_stream

        # A canceled message the target never received is void news: it must
        # not surface later as "new". A delivered one stays delivered history.
        xaacp_stream.supersede_undelivered(conn, message_id=message_id, now=answered_at)
        conn.commit()

    _audit_event(
        project_root,
        session_id,
        event_kind="stream_decide",
        action_kind="xaacp_cancel",
        target_entity=str(row["target_actor_id"]),
        payload={
            "message_id": message_id,
            "session_id": session_id,
            "lane_id": str(row["lane_id"]),
            "sender_actor_id": sender_actor_id,
            "target_actor_id": str(row["target_actor_id"]),
            "correlation_id": str(row["correlation_id"]),
            "reason": str(reason),
        },
    )
    return {
        "ok": True,
        "message_id": message_id,
        "status": "canceled",
    }
