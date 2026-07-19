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
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

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
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
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
            role TEXT NOT NULL
        )
    """)
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
    for old, new in (
        ("cerberus_role_map", "msg_role_map"),
        ("cerberus_reads", "msg_reads"),
    ):
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (old,),
        ).fetchone()
        if exists is not None:
            conn.execute(f"INSERT OR IGNORE INTO {new} SELECT * FROM {old}")
            conn.execute(f"DROP TABLE {old}")
    conn.execute("UPDATE messages SET direction = 'msg' WHERE direction = 'cerberus'")
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
    """Full picture for conductor: all lanes, states, pending questions, recent activity."""
    with _connect(project_root) as conn:
        # Lane states
        if session_id:
            lanes = conn.execute(
                "SELECT * FROM lane_control WHERE session_id = ? ORDER BY lane_id",
                (session_id,),
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
        recent = conn.execute("SELECT * FROM messages ORDER BY created_at DESC LIMIT 20").fetchall()

    return {
        "lanes": [
            {
                "lane_id": r["lane_id"],
                "state": r["state"],
                "reason": r["reason"] or "",
                "updated_at": r["updated_at"],
            }
            for r in lanes
        ],
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


def msg_register_role(project_root: Path, host_session_id: str, role: str) -> dict:
    """Register a host_session_id → role mapping for caller-role inference."""
    role = str(role or "").strip().lower()
    if role not in MSG_ROLES:
        raise ValueError(f"Invalid role '{role}'. Expected one of: {list(MSG_ROLES)}")
    with _connect(project_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO msg_role_map (host_session_id, role) VALUES (?, ?)",
            (host_session_id, role),
        )
        conn.commit()
    return {"host_session_id": host_session_id, "role": role}


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


def msg_send(
    project_root: Path,
    *,
    from_role: str,
    to_roles: object,
    body: str,
    in_reply_to: str = "",
    thread_id: str = "",
) -> dict:
    """Send a role-addressed message.

    Returns {id, thread_id}. If `in_reply_to` is set, derives thread_id
    from the original message (chains the thread).
    """
    from_role = str(from_role or "").strip().lower()
    if from_role not in MSG_ROLES:
        raise ValueError(f"Invalid from_role '{from_role}'. Expected one of: {list(MSG_ROLES)}")
    targets = _normalize_to_roles(to_roles)
    msg_id = str(uuid4())[:12]

    with _connect(project_root) as conn:
        resolved_thread = thread_id.strip()
        if in_reply_to:
            row = conn.execute(
                "SELECT thread_id, id FROM messages WHERE id = ?",
                (in_reply_to,),
            ).fetchone()
            if not row:
                raise ValueError(f"in_reply_to message '{in_reply_to}' not found")
            resolved_thread = (row["thread_id"] or row["id"]).strip()
        if not resolved_thread:
            resolved_thread = msg_id

        conn.execute(
            "INSERT INTO messages "
            "(id, lane_id, session_id, direction, category, content, status, created_at, "
            " from_role, to_roles_json, thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                msg_id,
                "",
                "",
                "msg",
                "chat",
                body,
                "pending",
                time.time(),
                from_role,
                json.dumps(targets),
                resolved_thread,
            ),
        )
        conn.commit()
    return {"id": msg_id, "thread_id": resolved_thread, "to_roles": targets}


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
) -> None:
    """Best-effort write to execution_events. Never raises."""
    try:
        from .execution_index_store import ExecutionIndexStore

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
_XAACP_MODES = frozenset({
    "xaacp_send", "xaacp_inbox", "xaacp_reply", "xaacp_wait", "xaacp_cancel",
})


def xaacp_resolve_caller_actor(project_root: Path) -> str:
    """Resolve one canonical actor from the live MCP caller; fail closed."""
    try:
        from .mcp_server_runtime_helpers import current_calling_host_session_id

        host_session_id = (current_calling_host_session_id() or "").strip()
    except Exception:
        host_session_id = ""
    if host_session_id:
        try:
            from .session_lane_agents_store import SessionLaneAgentsStore

            matches = [
                row for row in SessionLaneAgentsStore().get_all_lane_agents(project_root)
                if str(row.get("host_session_id") or "").strip() == host_session_id
            ]
            actor_ids = {
                str(row.get("worker_id") or "").strip()
                for row in matches
                if str(row.get("worker_id") or "").strip()
            }
            if len(actor_ids) == 1:
                return next(iter(actor_ids))
            if len(actor_ids) > 1:
                return MSG_ROLE_UNMAPPED
        except Exception:
            logger.exception("XAACP worker actor resolution failed")
            return MSG_ROLE_UNMAPPED
    role = msg_resolve_caller_role(project_root)
    return role if role in MSG_ROLES else MSG_ROLE_UNMAPPED


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
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
    wake: bool = False,
    metadata: dict | None = None,
    reason: str = "",
) -> dict:
    """Dispatch the XAACP modes of the canonical ai_msg contract."""
    selected = str(mode or "").strip().lower()
    if selected not in _XAACP_MODES:
        return {"ok": False, "status": "invalid", "error": "unknown XAACP mode"}
    actor_id = xaacp_resolve_caller_actor(project_root)
    if actor_id == MSG_ROLE_UNMAPPED:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "caller has no canonical XAACP actor binding",
        }
    supplied_target = str(target_actor_id or "").strip()
    if selected != "xaacp_send" and supplied_target and supplied_target != actor_id:
        return {
            "ok": False,
            "status": "forbidden",
            "error": "target_actor_id cannot override the canonical caller actor",
        }
    if selected == "xaacp_send":
        return xaacp_send(
            project_root,
            session_id=session_id,
            sender_actor_id=actor_id,
            target_actor_id=target_actor_id,
            lane_id=lane_id,
            message_kind=message_kind,
            body=body,
            correlation_id=correlation_id,
            reply_to_id=reply_to_id,
            metadata=metadata,
            wake=wake,
        )
    if selected == "xaacp_inbox":
        return xaacp_inbox(
            project_root,
            session_id=session_id,
            target_actor_id=actor_id,
            lane_id=lane_id,
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
        )
    if selected == "xaacp_reply":
        return xaacp_reply(
            project_root,
            message_id=message_id,
            session_id=session_id,
            responder_actor_id=actor_id,
            decision=decision,
            body=body,
        )
    if selected == "xaacp_wait":
        return xaacp_wait(
            project_root,
            message_id=message_id,
            session_id=session_id,
            actor_id=actor_id,
            timeout_seconds=timeout_seconds,
        )
    return xaacp_cancel(
        project_root,
        message_id=message_id,
        session_id=session_id,
        sender_actor_id=actor_id,
        reason=reason,
    )


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
    wake: bool = False,
    metadata: dict | None = None,
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
            unread_only=unread_only,
            mark_read=mark_read,
            limit=limit,
            wake=wake,
            metadata=metadata,
            reason=reason,
        )

    from_role = msg_resolve_caller_role(project_root)
    if from_role not in MSG_ROLES:
        if selected == "inbox":
            return {"role": from_role, "messages": []}
        if selected in {"send", "reply"}:
            return {
                "sent": False,
                "reason": (
                    f"caller is not a bound seat (role={from_role!r}); only the "
                    "conductor / co_conductor / king may post or read seat "
                    "messages. Bind first via ai_seat(mode='enter')."
                ),
            }
    if selected == "send":
        return msg_send(
            project_root,
            from_role=from_role,
            to_roles=to_roles,
            body=body,
            in_reply_to=in_reply_to,
        )
    if selected == "inbox":
        return {
            "role": from_role,
            "messages": msg_inbox(
                project_root,
                role=from_role,
                unread_only=unread_only,
            ),
        }
    if selected == "reply":
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT from_role, to_roles_json FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        if not row:
            return {"sent": False, "reason": f"Message '{message_id}' not found"}
        try:
            targets = json.loads(row["to_roles_json"] or "[]")
        except (TypeError, ValueError):
            targets = []
        recipients = [row["from_role"]] + [
            role for role in targets if role != from_role
        ]
        seen: list[str] = []
        for recipient in recipients:
            if recipient and recipient not in seen:
                seen.append(recipient)
        return msg_send(
            project_root,
            from_role=from_role,
            to_roles=seen or [row["from_role"]],
            body=body,
            in_reply_to=message_id,
        )
    valid = "send|inbox|reply|xaacp_send|xaacp_inbox|xaacp_reply|xaacp_wait|xaacp_cancel"
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
    }


def xaacp_send(
    project_root: Path,
    *,
    session_id: str,
    sender_actor_id: str,
    target_actor_id: str,
    lane_id: str,
    message_kind: str,
    body: str,
    correlation_id: str = "",
    reply_to_id: str = "",
    metadata: dict | None = None,
    wake: bool = False,
    ttl_seconds: float | None = None,
) -> dict:
    """Route an actor-attributed XAACP message through the canonical messages table."""
    invalid = _xaacp_required(
        session_id=session_id,
        sender_actor_id=sender_actor_id,
        target_actor_id=target_actor_id,
        lane_id=lane_id,
        message_kind=message_kind,
    )
    if invalid:
        return invalid
    session_id = str(session_id).strip()
    sender_actor_id = str(sender_actor_id).strip()
    target_actor_id = str(target_actor_id).strip()
    lane_id = str(lane_id).strip()
    message_kind = str(message_kind).strip()
    now = time.time()
    message_id = str(uuid4())[:12]
    correlation = str(correlation_id or "").strip() or message_id
    expires_at = now + max(0.0, float(ttl_seconds)) if ttl_seconds is not None else None
    with _connect(project_root) as conn:
        if reply_to_id:
            parent = conn.execute(
                "SELECT id, session_id FROM messages WHERE id=? AND direction='xaacp'",
                (reply_to_id,),
            ).fetchone()
            if parent is None or str(parent["session_id"]) != session_id:
                return {"ok": False, "status": "not_found", "message_id": reply_to_id}
        conn.execute(
            "INSERT INTO messages "
            "(id, lane_id, session_id, direction, category, content, status, "
            "created_at, protocol, message_kind, sender_actor_id, target_actor_id, "
            "correlation_id, reply_to_id, metadata_json, wake_requested, expires_at, "
            "thread_id) VALUES (?, ?, ?, 'xaacp', ?, ?, 'pending', ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?)",
            (
                message_id, lane_id, session_id, message_kind, str(body), now,
                _XAACP_PROTOCOL, message_kind, sender_actor_id, target_actor_id,
                correlation, str(reply_to_id or ""),
                json.dumps(dict(metadata or {}), sort_keys=True, default=str),
                1 if wake else 0, expires_at, correlation,
            ),
        )
        conn.commit()

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
        "reply_to_id": str(reply_to_id or ""),
        "wake_requested": bool(wake),
        "wake_queued": wake_queued,
        "mailbox_id": mailbox_id,
    }
    _audit_event(
        project_root, session_id, action_kind="xaacp_send",
        target_entity=target_actor_id, payload=audit_payload,
    )
    return {
        "ok": True,
        "message_id": message_id,
        "correlation_id": correlation,
        "status": "pending",
        "wake_queued": wake_queued,
        "mailbox_id": mailbox_id,
    }


def xaacp_inbox(
    project_root: Path,
    *,
    session_id: str,
    target_actor_id: str,
    lane_id: str,
    unread_only: bool = True,
    mark_read: bool = False,
    limit: int = 50,
) -> dict:
    """Read one exact XAACP route; never broaden across session, actor, or lane."""
    invalid = _xaacp_required(
        session_id=session_id, target_actor_id=target_actor_id, lane_id=lane_id
    )
    if invalid:
        return {**invalid, "messages": []}
    reader_key = "xaacp:" + str(target_actor_id).strip()
    now = time.time()
    with _connect(project_root) as conn:
        conn.execute(
            "UPDATE messages SET status='expired', decision_status='expired' "
            "WHERE direction='xaacp' AND status='pending' "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )
        sql = (
            "SELECT m.* FROM messages m "
            "LEFT JOIN msg_reads r ON r.message_id=m.id AND r.role=? "
            "WHERE m.direction='xaacp' AND m.session_id=? "
            "AND m.target_actor_id=? AND m.lane_id=?"
        )
        params: list[object] = [
            reader_key, str(session_id).strip(), str(target_actor_id).strip(),
            str(lane_id).strip(),
        ]
        if unread_only:
            sql += " AND r.message_id IS NULL"
        sql += " ORDER BY m.created_at ASC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(sql, params).fetchall()
        if mark_read and rows:
            conn.executemany(
                "INSERT OR IGNORE INTO msg_reads (message_id, role, read_at) "
                "VALUES (?, ?, ?)",
                [(row["id"], reader_key, now) for row in rows],
            )
        conn.commit()
    messages = [_xaacp_row(row) for row in rows]
    if mark_read and messages:
        _audit_event(
            project_root, str(session_id).strip(), action_kind="xaacp_read",
            target_entity=str(target_actor_id).strip(),
            payload={
                "session_id": str(session_id).strip(),
                "lane_id": str(lane_id).strip(),
                "target_actor_id": str(target_actor_id).strip(),
                "message_ids": [item["id"] for item in messages],
            },
        )
    return {"ok": True, "messages": messages}


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
        message_id=message_id, session_id=session_id,
        responder_actor_id=responder_actor_id, decision=decision,
    )
    if invalid:
        return invalid
    decision = str(decision).strip().lower()
    if decision not in _XAACP_DECISIONS:
        return {
            "ok": False, "status": "invalid",
            "error": f"decision must be one of {sorted(_XAACP_DECISIONS)}",
        }
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? "
            "AND direction='xaacp'",
            (str(message_id).strip(), str(session_id).strip()),
        ).fetchone()
        if row is None:
            return {"ok": False, "message_id": str(message_id).strip(), "status": "not_found"}
        if str(row["target_actor_id"]) != str(responder_actor_id).strip():
            return {"ok": False, "message_id": str(message_id).strip(), "status": "forbidden"}
        current = str(row["decision_status"] or row["status"] or "pending")
        if current in _XAACP_TERMINAL:
            return {
                "ok": True, "message_id": str(message_id).strip(),
                "status": current, "response": str(row["response"] or ""),
            }
        conn.execute(
            "UPDATE messages SET response=?, status=?, decision_status=?, "
            "answered_at=? WHERE id=?",
            (str(body), decision, decision, time.time(), str(message_id).strip()),
        )
        conn.commit()
    _audit_event(
        project_root, str(session_id).strip(), action_kind="xaacp_reply",
        target_entity=str(row["sender_actor_id"]),
        payload={
            "message_id": str(message_id).strip(),
            "session_id": str(session_id).strip(),
            "lane_id": str(row["lane_id"]),
            "sender_actor_id": str(row["sender_actor_id"]),
            "target_actor_id": str(row["target_actor_id"]),
            "responder_actor_id": str(responder_actor_id).strip(),
            "correlation_id": str(row["correlation_id"]),
            "decision": decision,
        },
    )
    return {
        "ok": True, "message_id": str(message_id).strip(),
        "status": decision, "response": str(body),
    }


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
        message_id=message_id, session_id=session_id, actor_id=actor_id
    )
    if invalid:
        return invalid
    deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), 30.0))
    while True:
        with _connect(project_root) as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE id=? AND session_id=? "
                "AND direction='xaacp'",
                (str(message_id).strip(), str(session_id).strip()),
            ).fetchone()
        if row is None:
            return {"ok": False, "message_id": str(message_id).strip(), "status": "not_found"}
        if str(actor_id).strip() not in {
            str(row["sender_actor_id"] or ""), str(row["target_actor_id"] or "")
        }:
            return {"ok": False, "message_id": str(message_id).strip(), "status": "forbidden"}
        status = str(row["decision_status"] or row["status"] or "pending")
        if status in _XAACP_TERMINAL:
            return {
                "ok": True, "message_id": str(message_id).strip(),
                "status": status, "response": str(row["response"] or ""),
            }
        if time.monotonic() >= deadline:
            return {
                "ok": True, "message_id": str(message_id).strip(),
                "status": "timeout", "response": "",
            }
        time.sleep(max(0.01, min(float(poll_interval_seconds), 0.25)))


def xaacp_cancel(
    project_root: Path,
    *,
    message_id: str,
    session_id: str,
    sender_actor_id: str,
    reason: str = "",
) -> dict:
    invalid = _xaacp_required(
        message_id=message_id, session_id=session_id,
        sender_actor_id=sender_actor_id,
    )
    if invalid:
        return invalid
    with _connect(project_root) as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE id=? AND session_id=? "
            "AND direction='xaacp'",
            (str(message_id).strip(), str(session_id).strip()),
        ).fetchone()
        if row is None:
            return {"ok": False, "message_id": str(message_id).strip(), "status": "not_found"}
        if str(row["sender_actor_id"]) != str(sender_actor_id).strip():
            return {"ok": False, "message_id": str(message_id).strip(), "status": "forbidden"}
        current = str(row["decision_status"] or row["status"] or "pending")
        if current in _XAACP_TERMINAL:
            return {"ok": True, "message_id": str(message_id).strip(), "status": current}
        conn.execute(
            "UPDATE messages SET response=?, status='canceled', "
            "decision_status='canceled', answered_at=? WHERE id=?",
            (str(reason), time.time(), str(message_id).strip()),
        )
        conn.commit()
    _audit_event(
        project_root, str(session_id).strip(), action_kind="xaacp_cancel",
        target_entity=str(row["target_actor_id"]),
        payload={
            "message_id": str(message_id).strip(),
            "session_id": str(session_id).strip(),
            "lane_id": str(row["lane_id"]),
            "sender_actor_id": str(sender_actor_id).strip(),
            "target_actor_id": str(row["target_actor_id"]),
            "correlation_id": str(row["correlation_id"]),
            "reason": str(reason),
        },
    )
    return {"ok": True, "message_id": str(message_id).strip(), "status": "canceled"}
