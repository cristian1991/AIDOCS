"""Conductor communication — SQLite-backed message queue between agents, conductors, and operators.

Three communication patterns:
1. Agent → Conductor/Operator: agent asks a question, waits for answer (conductor_ask)
2. Conductor/Operator → Agent: push guidance, agent sees it on next tool call (conductor_message)
3. Lane control: pause/resume/expand scope (conductor_lane_control)

SQLite is the transfer medium. Dashboard is the operator UI. Hooks inject messages into agent context.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


def _db_path(project_root: Path) -> Path:
    return project_root / ".MEMORY" / ".index" / "conductor_comms.sqlite3"


def _connect(project_root: Path) -> sqlite3.Connection:
    path = _db_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            lane_id TEXT NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            direction TEXT NOT NULL,  -- 'agent_to_conductor' | 'conductor_to_agent'
            category TEXT NOT NULL DEFAULT 'general',  -- 'question' | 'guidance' | 'scope_request' | 'status'
            content TEXT NOT NULL,
            response TEXT,
            status TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'answered' | 'read' | 'expired'
            created_at REAL NOT NULL,
            answered_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lane_control (
            lane_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'paused' | 'canceled'
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
    conn.commit()
    return conn


def register_lane_scope(project_root: Path, lane_id: str, files: list[str], *, session_id: str = "") -> None:
    """Register a lane's file scope. Called when conductor dispatches a lane."""
    normalized = [f.replace("\\", "/").strip() for f in files if f.strip()]
    with _connect(project_root) as conn:
        # Clear old scope for this lane
        conn.execute("DELETE FROM lane_scopes WHERE lane_id = ? AND session_id = ?", (lane_id, session_id))
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


def check_scope_conflict(project_root: Path, lane_id: str, file_path: str, session_id: str = "") -> str | None:
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
            (msg_id, lane_id, session_id, "agent_to_conductor", category, question, "pending", time.time()),
        )
        conn.commit()

    if not wait:
        return {"id": msg_id, "status": "pending", "message": "Question submitted to conductor. Continue with other work — the answer will appear as a conductor message on your next tool call once answered."}

    # Poll for answer
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _connect(project_root) as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if row and row["status"] == "answered":
                return {"id": msg_id, "status": "answered", "response": row["response"]}
        time.sleep(poll_interval)

    return {"id": msg_id, "status": "timeout", "message": f"No response within {timeout}s. Continue with your best judgment."}


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
            (msg_id, lane_id, session_id, "conductor_to_agent", "guidance", message, "pending", time.time()),
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
        original = conn.execute("SELECT lane_id, session_id FROM messages WHERE id = ?", (message_id,)).fetchone()
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
            (reply_id, original["lane_id"], original["session_id"], "conductor_to_agent", "answer", f"Re: your question — {response}", "pending", time.time()),
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
        return [{"id": row["id"], "category": row["category"], "content": row["content"]} for row in rows]


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
            {"id": row["id"], "lane_id": row["lane_id"], "category": row["category"],
             "content": row["content"], "created_at": row["created_at"]}
            for row in rows
        ]


# ── Lane Control ──

def set_lane_state(project_root: Path, lane_id: str, state: str, *, reason: str = "", session_id: str = "") -> dict:
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
        return {"lane_id": row["lane_id"], "state": row["state"], "reason": row["reason"] or ""}


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
    """Full picture for conductor: all lanes, states, pending questions, recent activity."""
    with _connect(project_root) as conn:
        # Lane states
        if session_id:
            lanes = conn.execute(
                "SELECT * FROM lane_control WHERE session_id = ? ORDER BY lane_id", (session_id,)
            ).fetchall()
        else:
            lanes = conn.execute("SELECT * FROM lane_control ORDER BY lane_id").fetchall()

        # Pending questions
        if session_id:
            questions = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' AND session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        else:
            questions = conn.execute(
                "SELECT * FROM messages WHERE direction = 'agent_to_conductor' AND status = 'pending' ORDER BY created_at",
            ).fetchall()

        # Recent messages (last 20)
        recent = conn.execute(
            "SELECT * FROM messages ORDER BY created_at DESC LIMIT 20"
        ).fetchall()

    return {
        "lanes": [
            {"lane_id": r["lane_id"], "state": r["state"], "reason": r["reason"] or "", "updated_at": r["updated_at"]}
            for r in lanes
        ],
        "pending_questions": [
            {"id": r["id"], "lane_id": r["lane_id"], "category": r["category"],
             "content": r["content"], "created_at": r["created_at"]}
            for r in questions
        ],
        "recent_messages": [
            {"id": r["id"], "lane_id": r["lane_id"], "direction": r["direction"],
             "category": r["category"], "content": r["content"][:100],
             "status": r["status"], "created_at": r["created_at"]}
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
        answer_question(project_root, message_id, f"Auto-approved: '{requested_path}' added to your scope. No conflicts with other lanes.")

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
            "id": r["id"], "lane_id": r["lane_id"], "direction": r["direction"],
            "category": r["category"], "content": r["content"],
            "response": r["response"], "status": r["status"],
            "created_at": r["created_at"], "answered_at": r["answered_at"],
        }
        for r in rows
    ]


# ── Task Routing ──

def resolve_backend_for_task(project_root: Path, task_type: str) -> dict:
    """Resolve the best backend + model for a task type based on conductor.task_routing config.
    
    Returns: {"backend": "claude"|"codex"|"opencode", "model": "provider/model" or ""}
    
    Task types: refactor, implement, design, test, docs, research, debug, review, deploy
    """
    try:
        from .config import get_setting
        import json as _json

        # Check task routing config
        routing_raw = get_setting("conductor.task_routing", project_root=project_root, default="{}")
        routing = _json.loads(routing_raw) if isinstance(routing_raw, str) else (routing_raw or {})

        if task_type in routing:
            spec = routing[task_type]
            # Format: "backend" or "backend/provider/model"
            parts = spec.split("/", 1) if isinstance(spec, str) else [spec]
            backend = parts[0]
            model = parts[1] if len(parts) > 1 else ""
            # Map backend aliases
            if backend in ("claude", "codex", "opencode"):
                return {"backend": backend, "model": model}
            # If it looks like a provider/model, assume opencode
            return {"backend": "opencode", "model": spec}

        # Fall back to default backend
        default_backend = str(get_setting("conductor.backend", project_root=project_root, default="claude"))
        default_model = str(get_setting("conductor.opencode_model", project_root=project_root, default="") or "")
        return {"backend": default_backend, "model": default_model if default_backend == "opencode" else ""}

    except Exception:
        return {"backend": "claude", "model": ""}
