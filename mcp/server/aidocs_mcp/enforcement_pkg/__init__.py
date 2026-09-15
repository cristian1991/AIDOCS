"""EnforcementController package — single doorway for safety law.

Castle law (Empire-rendered 2026-05-03):
    The emergency key must hang outside the prison cell, not inside it.

This package centralizes ALL shared gate / control / safety logic so
every host and surface obeys the same law via one canonical entrypoint.

Public surface:
    - EnforcementController.enforce(request) -> Decision
    - WriteAuthorizer.authorize_write(...)   (peer, not sub-controller)

Adapters (Claude hook, MCP dispatcher, OpenCode plugin, OpenAI Agents
adapter, dashboard action surface) translate host payloads into a
Request, call the controller, and shape the Decision into a
host-native response. Adapters do NOT decide safety.

Doctrine: see
    .MEMORY/sessions/2026-05-03-freeze-state-stickiness-firefighter/
    plans/enforcement-controller-doctrine.md

Inventory of legacy enforcement surfaces being migrated:
    .MEMORY/sessions/2026-05-03-freeze-state-stickiness-firefighter/
    plans/enforcement-surfaces-inventory.md

Status: skeleton phase. Models defined; gates and controller wiring
land after Empire resolutions on Q1 (resolved by #404: no kill switch),
Q2 (Freeze-before-Grants order), Q3 (degraded-state policy).
"""

from __future__ import annotations

# Re-exports — keep shallow so adapters can `from .enforcement_pkg
# import EnforcementController, Request, Decision` without reaching
# into submodules.
# (Concrete implementations land as gates are wired.)

__all__ = [
    "Decision",
    "DegradedState",
    "EnforcementController",
    "GateStep",
    "Request",
    "Surface",
    "Verdict",
    "WriteAuthorizer",
]

from .controller import EnforcementController
from .decision import Decision, DegradedState, GateStep, Verdict
from .surface import Request, Surface
from .write_authorizer import WriteAuthorizer
