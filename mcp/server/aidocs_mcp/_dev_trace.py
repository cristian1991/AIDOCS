"""Dev-flavor live-toggle trace helper.

Single entry point: ``trace(project_root, marker, *, run_id="", detail=None)``.

Wired into ai_run boundary breadcrumbs (server_run_tools._run_shell_unified
A-I, code_runner_detached.spawn_detached E*, shell_resolver.resolve_shell
R*). When the flag is off this is effectively a no-op — one TTL cache
read + one bool check, no file I/O.

Contract:
  - dev flavor only: ignored when distribution.flavor != "dev"
  - flag: dev.runtime.ai_run_trace (bool, default false)
  - live toggle: TTL cache (1s) on the resolved enabled-state so
    config_set takes effect within ~1s without MCP restart
  - never raises: any failure swallowed
  - never logs command/prompt content — caller is responsible for
    keeping ``marker`` and ``detail`` content-free; this helper does
    not introspect anything beyond what it's handed
  - log location: ``{project_root}/.MEMORY/.runs/_ai_run_trace_<pid>.log``
  - format: ``HH:MM:SS.mmm pid=<pid> [run_id=<id>] <marker>[ <detail>]``
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

# ── Live-toggle TTL cache ───────────────────────────────────────────
# 1-second TTL: live toggle works (config_set propagates within
# bounded time) but the hot path (every breadcrumb call) doesn't pay
# the sqlite read cost. Tuned for "an operator flipped the switch
# from the dashboard, expects markers to start within a tick or two".

_FLAG_TTL_SECONDS: float = 1.0
_FLAG_CACHE_LOCK = Lock()
# Keyed by project_root string. Value: (enabled_at_check, expires_at).
_FLAG_CACHE: dict[str, tuple[bool, float]] = {}


def _read_enabled_uncached(project_root: Path) -> bool:
    """Resolve the live state of the flag. Returns False on any
    failure — telemetry should never break the spawn path, and a
    failed read is "off" so we don't accidentally leak markers.
    """
    try:
        from .config import get_setting
    except Exception:
        return False

    # Dev-flavor gate. Even if the operator set the flag explicitly
    # on a stable/solo/corpo install, ignore it.
    try:
        flavor = (
            str(
                get_setting(
                    "distribution.flavor",
                    project_root=project_root,
                    default="solo",
                )
                or "solo",
            )
            .strip()
            .lower()
        )
    except Exception:
        return False
    if flavor != "dev":
        return False

    try:
        return bool(
            get_setting(
                "dev.runtime.ai_run_trace",
                project_root=project_root,
                default=False,
            ),
        )
    except Exception:
        return False


def trace_enabled(project_root: Path) -> bool:
    """Cheap live-toggle check. TTL cache (1s)."""
    key = str(project_root)
    now = time.time()
    with _FLAG_CACHE_LOCK:
        cached = _FLAG_CACHE.get(key)
        if cached is not None and now < cached[1]:
            return cached[0]
    enabled = _read_enabled_uncached(project_root)
    with _FLAG_CACHE_LOCK:
        _FLAG_CACHE[key] = (enabled, now + _FLAG_TTL_SECONDS)
    return enabled


def _invalidate_cache() -> None:
    """Test hook: drop the TTL cache so the next trace_enabled call
    re-reads config. Production code should rely on the natural TTL.
    """
    with _FLAG_CACHE_LOCK:
        _FLAG_CACHE.clear()


def _log_path(project_root: Path) -> Path:
    return Path(project_root) / ".MEMORY" / ".runs" / f"_ai_run_trace_{os.getpid()}.log"


def trace(
    project_root: Path,
    marker: str,
    *,
    run_id: str = "",
    detail: Any = None,
) -> None:
    """Record one trace line. No-op when the flag is off.

    Caller must ensure ``marker`` and ``detail`` carry no
    command/prompt content — this helper writes them verbatim.
    """
    try:
        if not trace_enabled(project_root):
            return
        path = _log_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%H:%M:%S")
        ms = int((time.time() % 1) * 1000)
        rid_part = f" run_id={run_id}" if run_id else ""
        detail_part = f" {detail}" if detail is not None else ""
        line = f"{ts}.{ms:03d} pid={os.getpid()}{rid_part} {marker}{detail_part}\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Never let telemetry break the call path.
        pass
