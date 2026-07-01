"""Session-deletion law — sealing .MEMORY/sessions deletion semantics.

DECISION (encoded 2026-05-24): session journals / SESSION.md / query-gate /
skills / session-bound memory under ``.MEMORY/sessions/<id>/`` are PROTECTED from
GENERIC deletion (see governed_deletion.classify_deletion → CAT_PROTECTED), so no
tool can erase them as ordinary project junk. They are CLEANUP-ONLY via the
single sanctioned admin path, which this law gates, fail-closed:

  * a meaningful reason (>=6 chars),
  * the target is NOT the active/current bound session (the active session's
    authority/memory is never deletable until unbound),
  * an authenticated operator context with admin permission,
  * CHECKPOINT-before-delete: the session files are quarantine-moved (a
    restorable removal) BEFORE the directory is removed — refuse if the restore
    point can't be secured,
  * AUDIT: intent recorded before, result after.

Legitimate GC/auto-clean keeps working: it runs through THIS sanctioned path (or
its own store APIs), never through generic governed-delete. Dependency-injected
so the law is unit/fuzz-testable without a real filesystem or server.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def session_deletion_guard(
    *,
    session_id: str,
    reason: str,
    is_active: bool,
    ctx: Any,
    has_permission: bool,
) -> dict[str, Any] | None:
    """Fail-closed admission for deleting a session. Returns a refusal dict, or
    None if every gate passes.
    """
    if not (session_id or "").strip():
        return {"ok": False, "blocked_by": "missing_session", "error": "session_id is required"}
    if is_active:
        return {
            "ok": False,
            "blocked_by": "active_session",
            "error": "the active/current bound session cannot be deleted; "
            "unbind it first (managed-mode-clear)",
        }
    if len((reason or "").strip()) < 6:
        return {
            "ok": False,
            "blocked_by": "reason_required",
            "error": "a meaningful reason (>=6 chars) is required to delete a session",
        }
    if ctx is None:
        return {
            "ok": False,
            "blocked_by": "auth_required",
            "error": "an authenticated operator is required to delete a "
            "session (local-default/unauthenticated callers refused)",
        }
    if not has_permission:
        return {
            "ok": False,
            "blocked_by": "permission_denied",
            "error": "admin permission required to delete a session",
        }
    return None


def run_session_deletion(
    *,
    session_id: str,
    reason: str,
    is_active: bool,
    ctx: Any,
    has_permission: bool,
    list_files: Callable[[], list[str]],
    quarantine_move: Callable[[list[str]], Any],
    remove_dir: Callable[[], None],
    record_intent: Callable[[], None],
    record_result: Callable[[Any], None],
) -> dict[str, Any]:
    """Guard → audit-intent (fail-closed) → checkpoint (quarantine-move) →
    remove emptied dir → audit-result (after, durable). Refuses to delete without
    a secured restore point.
    """
    refusal = session_deletion_guard(
        session_id=session_id,
        reason=reason,
        is_active=is_active,
        ctx=ctx,
        has_permission=has_permission,
    )
    if refusal is not None:
        return refusal

    # AUDIT INTENT — fail-closed (no unaudited session deletion).
    try:
        record_intent()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "blocked_by": "audit_intent_failed",
            "error": f"refusing to delete session: intent audit failed ({exc!r})",
        }

    files = list(list_files() or [])
    # CHECKPOINT-before-delete: quarantine-move is a restorable removal of the
    # session's files. Refuse the whole op if it can't be secured.
    cp = quarantine_move(files) if files else None
    if files and not getattr(cp, "ok", False):
        return {
            "ok": False,
            "blocked_by": "checkpoint_failed",
            "error": getattr(cp, "reason", "checkpoint failed"),
            "note": "refusing to delete a session without a restore point",
        }

    remove_dir()  # remove the now-empty session directory

    checkpoint_id = getattr(cp, "checkpoint_id", "") if cp else ""
    audit_result_ok = True
    try:
        record_result(cp)
    except Exception:  # noqa: BLE001 — deletion stands; intent already recorded
        audit_result_ok = False

    return {
        "ok": True,
        "session_id": session_id,
        "deleted": True,
        "reason": reason,
        "checkpoint_id": checkpoint_id,
        "files_checkpointed": len(files),
        "restore": (
            f"aidocs ai-restore restore --checkpoint {checkpoint_id}" if checkpoint_id else ""
        ),
        "audit_result_ok": audit_result_ok,
        "audit_degraded": (not audit_result_ok),
    }
