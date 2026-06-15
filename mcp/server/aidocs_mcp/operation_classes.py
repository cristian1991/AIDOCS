"""Operation-class registry — doctrine-driven gate behavior.

Castle / king doctrine 2026-05-04:
    Remedial operations (clear a freeze, approve/deny an escalation,
    kill a runaway run) are CLEANUP, not work. They cannot be locked
    behind the very freezes they manage.

This registry maps tool/CLI/dashboard-action names to an operation
class. Gates consult the registry to decide which gates apply. The
binding is HARDCODED here — not a caller-passed flag. Callers cannot
escalate their own privilege by passing `include_freeze=False`; they
must be in the registry.

Operation classes:

  - remedial_freeze_management
        Bypasses the FREEZE gate only.
        Still requires kill_switch+dev OR RBAC admin.
        Audit always.
        Examples: admin_clear_freeze, rbac_approve_escalation,
                  rbac_deny_escalation.

  - destructive_cleanup
        Bypasses the FREEZE gate only.
        For terminal-cleanup ops on running processes.
        Examples: ai_run_kill (kill a runaway).

  - normal
        Default. All gates apply.

If a tool is not in the registry, it is `normal`. Adding a tool to
a non-normal class is a doctrine decision; lint should require a
matching entry in `enforcement-controller-doctrine.md` § IV.

The registry is read-only; do not mutate at runtime.
"""

from __future__ import annotations

from enum import Enum


class OperationClass(str, Enum):
    NORMAL = "normal"
    REMEDIAL_FREEZE_MANAGEMENT = "remedial_freeze_management"
    DESTRUCTIVE_CLEANUP = "destructive_cleanup"


# Hardcoded mapping. Tool names are matched after stripping the
# "mcp__aidocs__" / "mcp__" namespace prefix so both bare and
# namespaced calls resolve to the same class.
_TOOL_OPERATION_CLASS: dict[str, OperationClass] = {
    "admin_clear_freeze": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "rbac_approve_escalation": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "rbac_deny_escalation": OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
    "ai_run_kill": OperationClass.DESTRUCTIVE_CLEANUP,
}


def operation_class_for(tool_name: str) -> OperationClass:
    """Resolve a tool's operation class. Default is NORMAL.

    Strips the standard MCP namespace prefixes so bare and namespaced
    forms collapse to the same class.
    """
    if not tool_name:
        return OperationClass.NORMAL
    norm = tool_name.strip().lower()
    for prefix in ("mcp__aidocs__", "mcp__"):
        if norm.startswith(prefix):
            norm = norm[len(prefix) :]
            break
    return _TOOL_OPERATION_CLASS.get(norm, OperationClass.NORMAL)


def bypasses_freeze_gate(tool_name: str) -> bool:
    """True if the tool is hardcoded as bypassing the freeze gate.

    Used by gate_tool.enforce_tool_call and ai_run_kill. The
    bypass is doctrine-bound, not caller-controlled.
    """
    cls = operation_class_for(tool_name)
    return cls in (
        OperationClass.REMEDIAL_FREEZE_MANAGEMENT,
        OperationClass.DESTRUCTIVE_CLEANUP,
    )
