"""Audit-deletion law — the AUDEL defense (highest-risk evidence-retention).

Deleting the execution audit trail can erase the proof of every other action, so
it is gated HARDER than ordinary admin mutation. Every audit-deletion
(execution_prune / execution_clear_token_usage / execution_clear_tool_calls)
must satisfy ALL of, fail-closed:

  * a meaningful reason (>=6 chars),
  * NOT a subagent (subagents may never erase audit evidence),
  * an authenticated operator context,
  * that operator holding an admin permission,
  * AUDIT-OF-AUDIT: the deletion is IMPOSSIBLE when the audit subsystem itself is
    degraded — if we cannot record the intent-to-delete, we must not delete the
    evidence.

Ordering: INTENT is recorded BEFORE deletion (the fail-closed probe + forensic
who/why), RESULT is recorded AFTER deletion (written post-clear so it SURVIVES a
clear-all and remains the durable record of what was erased).

Dependency-injected so the law is unit/fuzz-testable without booting the server.
NO network, NO new authority surface — reuses operator-auth/RBAC.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

AUDIT_DELETE_OPS = frozenset({"prune", "clear_token_usage", "clear_tool_calls"})


def audit_deletion_guard(
    *,
    operation: str,
    reason: str,
    ctx: Any,
    has_permission: bool,
    is_subagent: bool,
    record_intent: Callable[[], None],
) -> dict[str, Any] | None:
    """Fail-closed admission for an audit-deletion. Returns a refusal dict, or
    None if every gate passes (and the intent has been durably attempted).
    """
    op = (operation or "").strip().lower()
    if op not in AUDIT_DELETE_OPS:
        return {
            "ok": False,
            "blocked_by": "unknown_operation",
            "error": f"unknown audit-deletion {operation!r}",
            "operations": sorted(AUDIT_DELETE_OPS),
        }
    if len((reason or "").strip()) < 6:
        return {
            "ok": False,
            "blocked_by": "reason_required",
            "error": "a meaningful reason (>=6 chars) is required to delete audit evidence",
        }
    if is_subagent:
        return {
            "ok": False,
            "blocked_by": "subagent_forbidden",
            "error": "subagents may never delete audit evidence",
        }
    if ctx is None:
        return {
            "ok": False,
            "blocked_by": "auth_required",
            "error": "an authenticated operator is required to delete audit "
            "evidence (local-default/unauthenticated callers refused)",
        }
    if not has_permission:
        return {
            "ok": False,
            "blocked_by": "permission_denied",
            "error": "admin permission required to delete audit evidence",
        }
    # AUDIT-OF-AUDIT, fail-closed: you cannot delete the audit trail if you
    # cannot first record that you are deleting it.
    try:
        record_intent()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "blocked_by": "audit_subsystem_degraded",
            "error": f"refusing to delete audit evidence: the audit subsystem "
            f"cannot record the deletion intent ({exc!r})",
        }
    return None


def run_audit_deletion(
    *,
    operation: str,
    reason: str,
    ctx: Any,
    has_permission: bool,
    is_subagent: bool,
    record_intent: Callable[[], None],
    record_result: Callable[[Any], None],
    deleter: Callable[[], Any],
) -> dict[str, Any]:
    """Guard → delete → record result (after, durable). Degraded-tolerant on the
    result record (the deletion stands; intent was already recorded).
    """
    refusal = audit_deletion_guard(
        operation=operation,
        reason=reason,
        ctx=ctx,
        has_permission=has_permission,
        is_subagent=is_subagent,
        record_intent=record_intent,
    )
    if refusal is not None:
        return refusal
    result = deleter()
    audit_result_ok = True
    try:
        record_result(result)
    except Exception:  # noqa: BLE001 — deletion stands; report degraded
        audit_result_ok = False
    return {
        "ok": True,
        "operation": operation.strip().lower(),
        "result": result,
        "reason": reason,
        "audit_result_ok": audit_result_ok,
        "audit_degraded": (not audit_result_ok),
    }
