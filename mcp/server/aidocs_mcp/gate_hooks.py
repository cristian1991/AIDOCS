"""The gate-hooks seam -- a neutral public registry with NO gate imports.

Design decision (2026-10-29): every outer-gate file is private and no public
module may import one. A few public modules still carry a branch that only
means something on the gate (the org-admin verdict for a gate principal, the
OAuth gate-only clearance, the gate's org-credential git transport, the gate's
surface-scoped project selection). Those branches call a hook from this
registry instead of importing the gate.

The private gate boots the seam at startup (``outer_gate_hooks.register_gate_hooks``,
called from ``OuterGate`` and the gate server entry): ``boot`` registers every
hook, fails startup if any is missing, sets the boot marker and SEALS the
table, so nothing can replace or clear a hook afterwards. A public build has
nothing registered and no boot marker, so ``get`` returns None and every
caller answers fail CLOSED with the typed ``gate_only`` result from
``gate_only.py`` -- an explicit refusal, never a crash and never a guess. A
caller whose missing hook would otherwise mean ALLOW checks ``gate_booted()``
and its own configuration and refuses instead.

This module imports only the standard library. It names the hooks; it does not
know what any of them does.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable

#: (principal: dict) -> bool. Is this gate principal an org admin?
ORG_ADMIN = "org_admin"
#: (auth_home: Path, client_id: str) -> bool. Is this OAuth client still
#: registered as gate-only by the canonical record?
OAUTH_CLEARED_GATE_ONLY = "oauth_cleared_gate_only"
#: (project_root, tenant_id: str, git_local) -> drift runner. The origin drift
#: runner over the tenant's org credential.
GATE_GIT_DRIFT_RUNNER = "gate_git_drift_runner"
#: (op, *, project_root, principal, remote, refspec, hub, git_local) -> dict.
#: ai_git fetch/pull/push for a gate principal.
RUN_GATE_GIT_OP = "run_gate_git_op"
#: () -> str. The surface ref bound for the current gate request.
GATE_SURFACE_REF = "gate_surface_ref"
#: (home, user_id, principal, *, surface_ref, binding_home) -> (status, project).
#: The gate's surface-scoped, allowlist-checked project selection.
GATE_PROJECT_SELECTION = "gate_project_selection"
#: () -> dict | None. The web run axis, or None when it is off. When on:
#: {"host_kind", "primary", "unclassified", "agent_prefix"}.
WEB_RUN_AXIS = "web_run_axis"

HOOK_NAMES: frozenset[str] = frozenset(
    {
        ORG_ADMIN,
        OAUTH_CLEARED_GATE_ONLY,
        GATE_GIT_DRIFT_RUNNER,
        RUN_GATE_GIT_OP,
        GATE_SURFACE_REF,
        GATE_PROJECT_SELECTION,
        WEB_RUN_AXIS,
    }
)

_LOCK = threading.Lock()
_HOOKS: dict[str, Callable[..., Any]] = {}
#: Set by boot(): this process IS a gate that booted its hooks. Lets a caller
#: tell "public build, nothing configured" from "the gate's hook is missing".
_BOOTED = False
#: Set by boot(): the table can no longer be replaced or cleared.
_SEALED = False


class GateHooksSealed(RuntimeError):
    """The gate sealed the hook table at startup; it cannot be changed now."""


class GateHooksNotReady(RuntimeError):
    """Gate startup: a required hook is missing or not callable."""


def register(name: str, fn: Callable[..., Any]) -> None:
    """Register (or replace) one hook. Refused once the gate has sealed."""
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown gate hook {name!r}")
    if not callable(fn):
        raise TypeError(f"gate hook {name!r} must be callable")
    with _LOCK:
        if _SEALED:
            raise GateHooksSealed(f"gate hooks are sealed; {name!r} cannot be replaced")
        _HOOKS[name] = fn


def get(name: str) -> Callable[..., Any] | None:
    """The registered implementation, or None (the caller then fails closed)."""
    if name not in HOOK_NAMES:
        raise ValueError(f"unknown gate hook {name!r}")
    with _LOCK:
        return _HOOKS.get(name)


def registered() -> frozenset[str]:
    with _LOCK:
        return frozenset(_HOOKS)


def gate_booted() -> bool:
    """True in a gate process whose hooks booted (the boot marker)."""
    return _BOOTED


def sealed() -> bool:
    return _SEALED


def _missing(table: dict[str, Callable[..., Any]]) -> list[str]:
    return sorted(n for n in HOOK_NAMES if not callable(table.get(n)))


def boot(implementations: dict[str, Callable[..., Any]]) -> None:
    """Gate startup: register every hook, check readiness, then seal.

    Raises GateHooksNotReady (and leaves nothing booted or sealed) when any
    required hook is missing or not callable, so the gate fails to START
    rather than serve with a hole. Idempotent: on an already sealed, ready
    table it changes nothing.
    """
    global _BOOTED, _SEALED
    unknown = sorted(set(implementations) - HOOK_NAMES)
    if unknown:
        raise GateHooksNotReady(f"unknown gate hook(s): {unknown}")
    with _LOCK:
        if _SEALED:
            missing = _missing(_HOOKS)
            if missing:
                raise GateHooksNotReady(f"sealed gate hooks are missing {missing}")
            return
        missing = _missing(implementations)
        if missing:
            raise GateHooksNotReady(f"gate startup: required hook(s) missing: {missing}")
        _HOOKS.clear()
        _HOOKS.update(implementations)
        _BOOTED = True
        _SEALED = True


def clear() -> None:
    """Drop every hook: the state of a public build. Refused once sealed."""
    with _LOCK:
        if _SEALED:
            raise GateHooksSealed("gate hooks are sealed; the table cannot be cleared")
        _HOOKS.clear()


@contextmanager
def overridden(hooks: dict[str, Callable[..., Any]], *, booted: bool) -> Iterator[None]:
    """TESTS ONLY: swap the whole table and the boot marker for one block, then
    restore both. Inside, the table is unsealed (so register() works) unless
    the block itself boots. Refused outside a pytest run, so production code
    has no path to unseal the gate's table."""
    global _BOOTED, _SEALED
    if "pytest" not in sys.modules:
        raise GateHooksSealed("gate_hooks.overridden is a test-only context")
    with _LOCK:
        saved = (dict(_HOOKS), _BOOTED, _SEALED)
        _HOOKS.clear()
        _HOOKS.update(hooks)
        _BOOTED = booted
        _SEALED = False
    try:
        yield
    finally:
        with _LOCK:
            _HOOKS.clear()
            _HOOKS.update(saved[0])
            _BOOTED, _SEALED = saved[1], saved[2]
