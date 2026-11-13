"""Audit-deletion law — the AUDEL defense (highest-risk evidence-retention).

Deleting the execution audit trail can erase the proof of every other action, so
it is gated HARDER than ordinary admin mutation. Every audit-deletion
(execution_prune / execution_clear_tool_calls / the store's clear_all, plus
execution_clear_token_usage, which since #885 hides figures behind an
append-only watermark rather than deleting, and stays gated because hiding audit
figures is still an operator act that must carry a reason and a name)
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
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: "clear_all" joined the set with the #885 chokepoint: ExecutionIndexStore.
#: clear_all wipes the WHOLE ledger and had no law over it at all — it simply
#: had no production caller yet, which is not a defence.
AUDIT_DELETE_OPS = frozenset(
    {"prune", "clear_token_usage", "clear_tool_calls", "clear_all"},
)


# ── The chokepoint (#885) ────────────────────────────────────────────────
# The law used to be advisory: it wrapped ONE caller (the MCP tool) while the
# store's delete methods stayed importable by anyone. The Tauri dashboard, the
# OpenCode plugin and two in-process callers each reached
# ``ExecutionIndexStore`` directly and deleted from the append-only ledger with
# no operator, no reason and no audit-of-audit.
#
# A WARRANT closes that. It exists only for the duration of
# ``run_audit_deletion``'s call to its ``deleter``, it is carried in a ContextVar
# (so no signature in the call chain changes and no caller can forge one by
# passing an argument), and the store's delete methods call
# :func:`require_warrant` before touching a row. Anything that reaches the store
# outside the law raises :class:`UngovernedAuditDeletion` BEFORE the DELETE
# executes — fail-closed, never a partial delete.


class UngovernedAuditDeletion(RuntimeError):
    """A delete against the audit ledger was attempted outside the law."""


@dataclass(frozen=True)
class AuditDeletionWarrant:
    """Proof that THIS deletion passed THIS law, for THIS operation."""

    operation: str
    reason: str


_ACTIVE_WARRANT: ContextVar[AuditDeletionWarrant | None] = ContextVar(
    "aidocs_active_audit_deletion_warrant",
    default=None,
)


def require_warrant(operation: str) -> AuditDeletionWarrant:
    """Admission for a store-level audit deletion. Raises unless a warrant for
    exactly ``operation`` is in force.

    Deliberately RAISES rather than returning a refusal dict: this is called
    from inside the store, where a caller that ignored a returned value would go
    on to delete anyway.
    """
    op = (operation or "").strip().lower()
    warrant = _ACTIVE_WARRANT.get()
    if warrant is None:
        raise UngovernedAuditDeletion(
            f"refusing {op!r}: the execution audit ledger is append-only and "
            "hash-chained, so deleting from it is only lawful through "
            "audit_deletion_law.run_audit_deletion (authenticated operator + "
            "admin permission + reason + audit-of-audit). This caller reached "
            "the store directly.",
        )
    if warrant.operation != op:
        raise UngovernedAuditDeletion(
            f"refusing {op!r}: the warrant in force authorises "
            f"{warrant.operation!r}. A warrant is not transferable between "
            "audit-deletion operations.",
        )
    return warrant


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
    # The warrant is in force ONLY across this call. Scoping it to the deleter
    # (and resetting in `finally`) is what stops a passed law call from leaving
    # the store unlocked for whatever runs next on this task.
    token = _ACTIVE_WARRANT.set(
        AuditDeletionWarrant(operation=operation.strip().lower(), reason=reason),
    )
    try:
        result = deleter()
    finally:
        _ACTIVE_WARRANT.reset(token)
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
