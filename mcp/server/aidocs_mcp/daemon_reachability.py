"""Half-open gate detection (#432 residual): hooks active, tool-surface down.

When the hook layer is alive (PreToolUse still fires) but the aidocs MCP
daemon is unreachable (crashed / stopped / hot-swap window), every gate
refusal used to be a GENERIC block: the agent was told to use
``mcp__aidocs__`` tools that do not exist right now. FAIL-OPEN IS
FORBIDDEN here — the gate keeps denying (security stays) — but the
refusal must be LOUD and NAMED: it carries the actual condition and the
recovery path, and ONE deduped health event lands in the audit stream
per outage.

Wired at the two refusal chokepoints every hook-side deny flows through:
``access_gate.GateDecision.__post_init__`` and
``tool_gate_service.ToolGateResult.deny``. Presentation-only — this
module NEVER changes a verdict and NEVER raises into a gate.
"""

from __future__ import annotations

import json
from pathlib import Path

DAEMON_UNREACHABLE_RULE_ID = "daemon_unreachable_half_open"

# The named condition + recovery. Kept as a single recognizable sentence so
# tests (and operators) can grep for it, and so decorate_refusal can stay
# idempotent by substring check.
DAEMON_UNREACHABLE_NOTICE = (
    "aidocs daemon unreachable — hooks active but tool-surface down "
    "(daemon {status}). This refusal stands (fail-closed; security stays), "
    "but the aidocs tools it points at are NOT reachable right now. "
    "Recovery: reconnect the MCP server (/mcp) once the daemon is back "
    "(aidocs service start / deploy hot-swap settling); operator escape "
    "hatch: hooks_failsafe."
)

# Statuses written by aidocs_service.write_daemon_health that mean the
# tool-surface is down while hooks may still be firing.
_DOWN_STATUSES = frozenset({"stopped", "crash_looped", "down"})


def _marker_path() -> Path:
    from .aidocs_service import daemon_dir

    return daemon_dir() / "unreachable_reported.json"


def unreachable_status() -> str | None:
    """Short status string when the daemon tool-surface is down, else None.

    Conservative by design: an outage is only claimed when the daemon
    health file EXISTS and says not-up, or says up but its recorded pid is
    dead (crash / hot-swap window before the watchdog rewrites health).
    No health file (stdio installs, never-commissioned box) -> None.
    Never raises.
    """
    try:
        from .aidocs_service import _pid_alive, read_daemon_health

        health = read_daemon_health()
        if not isinstance(health, dict):
            return None
        status = str(health.get("status") or "").strip().lower()
        if status in _DOWN_STATUSES:
            return status
        if status == "up":
            pid = int(health.get("pid") or 0)
            if pid > 0 and not _pid_alive(pid):
                return "up-but-pid-dead"
        return None
    except Exception:  # noqa: BLE001 — reachability probe must never wedge a gate
        return None


def _emit_health_event_once(status: str, project_root: Path | None = None) -> bool:
    """Record ONE ``daemon_unreachable`` audit event per outage identity.

    Dedup is a tiny marker file next to the daemon health file: while the
    marker records the same status, subsequent refusals stay silent. The
    marker is cleared by ``decorate_refusal`` when health recovers, so the
    NEXT outage emits again. Returns True when an event was written.
    Never raises.
    """
    try:
        marker = _marker_path()
        prev = None
        try:
            prev = json.loads(marker.read_text(encoding="utf-8")).get("status")
        except Exception:  # noqa: BLE001 — absent/corrupt marker == not reported
            prev = None
        if prev == status:
            return False
        marker.write_text(json.dumps({"status": status}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    try:
        root = Path(project_root) if project_root else None
        if root is None:
            from .mcp_server_runtime_helpers import resolve_project_root

            root = resolve_project_root()
        from .execution_index_store import ExecutionIndexStore

        ExecutionIndexStore().record_event(
            root,
            "daemon_unreachable",
            "hook_gate",
            action_kind="health",
            status="down",
            payload={"daemon_status": status, "rule_id": DAEMON_UNREACHABLE_RULE_ID},
        )
        return True
    except Exception:  # noqa: BLE001 — audit best-effort; the refusal text is the loud part
        return False


def _clear_marker() -> None:
    try:
        _marker_path().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def decorate_refusal(reason: str, project_root: Path | None = None) -> str:
    """Append the named half-open condition to a gate refusal when the
    daemon is unreachable. Verdict-neutral (the deny stands either way),
    idempotent, and never raises.
    """
    try:
        message = str(reason or "")
        status = unreachable_status()
        if status is None:
            _clear_marker()
            return message
        _emit_health_event_once(status, project_root)
        if "aidocs daemon unreachable" in message:
            return message
        notice = DAEMON_UNREACHABLE_NOTICE.format(status=status)
        return f"{message}\n⚠ {notice}"
    except Exception:  # noqa: BLE001
        return str(reason or "")
