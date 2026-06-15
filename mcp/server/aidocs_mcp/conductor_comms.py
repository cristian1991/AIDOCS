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
    # Message substrate — additive columns on `messages` for
    # role-addressed messaging available to all agents (conductor /
    # co_conductor / king today; expandable). Phoenix 2026-05-12:
    # renamed from "cerberus" per king directive — single canonical
    # name (msg_*) end-to-end, no internal-vs-external split.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "from_role" not in existing_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN from_role TEXT NOT NULL DEFAULT ''")
    if "to_roles_json" not in existing_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN to_roles_json TEXT NOT NULL DEFAULT '[]'")
    if "thread_id" not in existing_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN thread_id TEXT NOT NULL DEFAULT ''")
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
    # Phoenix 2026-05-12: one-shot migration from cerberus_* names.
    # Idempotent — only fires when legacy tables/rows still exist.
    for _old, _new in (
        ("cerberus_role_map", "msg_role_map"),
        ("cerberus_reads", "msg_reads"),
    ):
        _exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (_old,),
        ).fetchone()
        if _exists is not None:
            conn.execute(f"INSERT OR IGNORE INTO {_new} SELECT * FROM {_old}")
            conn.execute(f"DROP TABLE {_old}")
    # Migrate messages.direction tag value 'cerberus' → 'msg' so old
    # rows surface under the new tag and the SQL filters below stay
    # uniform. Idempotent — UPDATE matches nothing on subsequent runs.
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
# Phoenix 2026-05-12: renamed from cerberus_* (king directive — one
# canonical name end-to-end, no internal-vs-external split).

MSG_ROLES = ("conductor", "co_conductor", "king")


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
    """Resolve the calling agent's role.

    Reads host_session_id from the current MCP context and looks it up
    in msg_role_map. Defaults to 'conductor' for the bound conductor
    seat when no mapping is registered.
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
    return "conductor"


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
