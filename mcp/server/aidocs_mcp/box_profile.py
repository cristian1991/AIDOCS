"""Box-adaptive execution budgets — ONE machine-profile authority (#456).

Emperor decree 2026-07-18: "core-count/worker usage should depend on box —
my limitations should not apply for huge dev/production boxes." This module
computes the box profile ONCE per process and every governed runner derives
its budgets from it:

  - pytest worker counts (``-n``) for ai_test — the small 4-core operator
    box keeps the courtesy decree (agents -n 2 max, conductor serial); big
    dev/production boxes scale automatically from the same formula:
        total_budget      = max(2, floor(cpu * 0.75))
        conductor_reserve = 1 on small boxes, ceil(total/4) on big ones
        agent_cap         = total - conductor_reserve
    (4-core → 3 total: 2 agents + serial conductor; 8-core → 6: 4+2;
     32-core → 24: 18 agents + 6 conductor — the #456 verification matrix.)
  - detached-run timeout CEILINGS for ai_run/ai_test (#466) — the caller's
    ``timeout_seconds`` is honored EXACTLY up to the box ceiling; an
    over-ceiling request is REFUSED explicitly, naming the ceiling. Never a
    silent clamp.

Security posture: the profile only scales BUDGETS (worker counts, timeout
ceilings). It can never disable the judge, the gate cascade, or any policy
floor — those run regardless of box size.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Absolute safety rail — no box profile and no config value may push a
# detached run's timeout ceiling past this (a misconfig must not leave a
# subprocess pinned for days). Mirrors code_runner_detached's historical
# MAX_RUN_TIMEOUT_CEILING; defined here so this module owns ceiling law
# without a circular import.
HARD_TIMEOUT_CEILING_SECONDS = 3600

# Box-derived default ceilings (used when the operator has NOT explicitly
# set `run.max_timeout_seconds`). Small operator boxes get a tighter rail;
# big boxes may run long suites in one governed shot.
SMALL_BOX_TIMEOUT_CEILING_SECONDS = 1800
BIG_BOX_TIMEOUT_CEILING_SECONDS = HARD_TIMEOUT_CEILING_SECONDS

# A box is "small" (operator courtesy rules apply) at or below this many
# logical cores.
SMALL_BOX_MAX_CORES = 4


@dataclass(frozen=True)
class BoxProfile:
    """Immutable machine profile — computed once, cached for the process."""

    cpu_count: int
    memory_gb: float | None
    memory_class: str  # "small" | "standard" | "large" | "unknown"
    name: str  # "small-operator" | "big-box"
    total_worker_budget: int
    conductor_worker_slots: int  # 1 on small boxes == serial conductor
    agent_worker_cap: int
    timeout_ceiling_seconds: int

    @property
    def is_small(self) -> bool:
        return self.name == "small-operator"


def _detect_memory_gb() -> float | None:
    """Best-effort physical RAM detection without hard psutil dependency."""
    try:
        import psutil  # type: ignore[import-not-found]

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:
        pass
    try:
        if os.name == "nt":
            import ctypes

            class _MemStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemStatus()
            status.dwLength = ctypes.sizeof(_MemStatus)
            windll = getattr(ctypes, "windll", None)
            if windll is not None and windll.kernel32.GlobalMemoryStatusEx(
                ctypes.byref(status),
            ):
                return round(status.ullTotalPhys / (1024**3), 1)
            return None
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        return round(page * pages / (1024**3), 1)
    except Exception:
        return None


def _memory_class(memory_gb: float | None) -> str:
    if memory_gb is None:
        return "unknown"
    if memory_gb < 8:
        return "small"
    if memory_gb < 32:
        return "standard"
    return "large"


def compute_box_profile(
    cpu_count: int,
    memory_gb: float | None = None,
) -> BoxProfile:
    """Pure profile math — the #456 formula. Testable with mocked cores."""
    cpu = max(1, int(cpu_count))
    total = max(2, math.floor(cpu * 0.75))
    # Memory can only TIGHTEN the budget (roughly one worker per 2GB) —
    # a RAM-starved many-core box must not thrash.
    if memory_gb is not None:
        total = max(1, min(total, int(memory_gb // 2) or 1))
    small = cpu <= SMALL_BOX_MAX_CORES
    conductor = 1 if small else max(1, math.ceil(total / 4))
    agent_cap = max(1, total - conductor)
    if small:
        # Courtesy decree floor: agents never exceed -n 2 on the
        # operator's small box, whatever the memory math said.
        agent_cap = min(agent_cap, 2)
    return BoxProfile(
        cpu_count=cpu,
        memory_gb=memory_gb,
        memory_class=_memory_class(memory_gb),
        name="small-operator" if small else "big-box",
        total_worker_budget=total,
        conductor_worker_slots=conductor,
        agent_worker_cap=agent_cap,
        timeout_ceiling_seconds=(
            SMALL_BOX_TIMEOUT_CEILING_SECONDS if small else BIG_BOX_TIMEOUT_CEILING_SECONDS
        ),
    )


_PROFILE_LOCK = threading.Lock()
_CACHED_PROFILE: BoxProfile | None = None


def get_box_profile() -> BoxProfile:
    """The process-wide box profile — computed once, then cached."""
    global _CACHED_PROFILE
    with _PROFILE_LOCK:
        if _CACHED_PROFILE is None:
            _CACHED_PROFILE = compute_box_profile(
                os.cpu_count() or 1,
                _detect_memory_gb(),
            )
        return _CACHED_PROFILE


def reset_box_profile_cache() -> None:
    """Test-only: drop the cached profile so the next call recomputes."""
    global _CACHED_PROFILE
    with _PROFILE_LOCK:
        _CACHED_PROFILE = None


# ── Timeout governance (#466) ───────────────────────────────────────────


def effective_timeout_ceiling(project_root: Path | None) -> int:
    """The box's detached-run timeout ceiling.

    An operator-set `run.max_timeout_seconds` (global/project/session
    layer) is EXPLICIT policy and wins; otherwise the ceiling derives
    from the box profile. Always clamped to the hard safety rail.
    """
    ceiling = get_box_profile().timeout_ceiling_seconds
    try:
        from .config_resolver import LayeredConfigResolver

        resolved = LayeredConfigResolver().resolve(
            "run.max_timeout_seconds",
            project_root,
        )
        origin = resolved.origin.get("run.max_timeout_seconds", "")
        if origin and origin != "factory" and resolved.value is not None:
            ceiling = int(resolved.value)
    except Exception:
        pass
    return max(1, min(ceiling, HARD_TIMEOUT_CEILING_SECONDS))


def resolve_run_timeout(
    requested_seconds: int,
    *,
    tool_default: int,
    project_root: Path | None,
) -> dict[str, Any]:
    """Honest timeout resolution for a governed run (#466).

    Returns {"ok": True, timeout_seconds, timeout_governed_by,
    timeout_ceiling_seconds, box_profile} when the request is within the
    box ceiling — the caller's value is honored EXACTLY (never clamped).
    Over-ceiling requests get {"ok": False, blocked_by:
    "timeout_over_ceiling", ...} with a message that NAMES the ceiling —
    an explicit refusal, never a silent clamp.
    """
    profile = get_box_profile()
    ceiling = effective_timeout_ceiling(project_root)
    try:
        requested = int(requested_seconds)
    except (TypeError, ValueError):
        requested = 0
    if requested <= 0:
        requested = int(tool_default)
    if requested > ceiling:
        return {
            "ok": False,
            "blocked_by": "timeout_over_ceiling",
            "err": (
                f"requested timeout {requested}s exceeds this box's ceiling "
                f"of {ceiling}s (box profile {profile.name}, "
                f"{profile.cpu_count} cores). Refusing rather than silently "
                f"clamping — re-request with timeout_seconds <= {ceiling}, "
                f"split the run, or have the operator raise "
                f"run.max_timeout_seconds (hard max "
                f"{HARD_TIMEOUT_CEILING_SECONDS}s)."
            ),
            "requested_timeout_seconds": requested,
            "timeout_ceiling_seconds": ceiling,
            "box_profile": profile.name,
        }
    return {
        "ok": True,
        "timeout_seconds": requested,
        "timeout_governed_by": ("default" if requested == int(tool_default) else "requested"),
        "timeout_ceiling_seconds": ceiling,
        "box_profile": profile.name,
    }


# ── Test-worker governance (#456) ───────────────────────────────────────

_XDIST_CONFIG_FILES = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini")


def _xdist_configured(run_dir: Path) -> bool:
    """True when the project under *run_dir* already uses pytest-xdist.

    ai_test only steers a worker count where xdist is part of the
    project's own test config — injecting ``-n`` into a project without
    the plugin would break its suite.
    """
    for name in _XDIST_CONFIG_FILES:
        try:
            text = (run_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "xdist" in text or "-n auto" in text:
            return True
    return False


def resolve_test_workers(
    framework: str,
    run_dir: Path,
    *,
    is_lane_worker: bool,
    profile: BoxProfile | None = None,
) -> tuple[int | None, str]:
    """(xdist ``-n`` value or None, human rationale) for an ai_test run.

    None means "do not steer workers" (non-pytest framework, or xdist not
    configured). 0 means explicit serial (overrides an addopts ``-n auto``).
    """
    profile = profile or get_box_profile()
    if framework != "pytest":
        return None, f"{framework}: no xdist worker steering (pytest-only concern)"
    if not _xdist_configured(run_dir):
        return None, "pytest-xdist not configured for this project; leaving pytest defaults"
    slots = profile.agent_worker_cap if is_lane_worker else profile.conductor_worker_slots
    n = 0 if slots <= 1 else slots
    role = "agent lane" if is_lane_worker else "conductor"
    return n, (
        f"box profile {profile.name} ({profile.cpu_count} cores, total "
        f"budget {profile.total_worker_budget}): {role} gets {slots} "
        f"slot(s) -> -n {n}" + (" (serial)" if n == 0 else "")
    )
