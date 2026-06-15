"""ClearFreezeService — the ONE audited primitive for lifting a freeze.

Every surface that clears a session freeze — the CLI (`aidocs admin
clear-freeze`), the MCP `admin_clear_freeze` tool, and the operator
chat-unfreeze — clears THROUGH ``clear_with_audit`` so the ordering and the
audit ledger are identical and a partial failure is never a silent
inconsistency.

Castle-grade ordering across three NON-transactional sqlite stores
(execution_index / escalation / session_freeze):

  1. write the AUDIT LEDGER first (``freeze_clear_initiated``). If even this
     fails → KEEP the freeze, escalation untouched (fully consistent).
  2. decline the linked escalation. On failure → ``freeze_clear_degraded``
     marker + KEEP the freeze (request stays pending = consistent frozen
     state).
  3. lift the lock. On failure the escalation is already declined while the
     freeze is still active — the one inconsistent edge: emit an explicit
     ``freeze_clear_repair_needed`` marker + KEEP the freeze so a repair
     pass / existing-freeze rendering reconciles it.
  4. write the surface's ``cleared`` event on success.

Callers own resolution + authorization (by id/session, RBAC / dev-flavor /
host-binding); the service owns the atomic-ish decide+clear+audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ClearFreezeResult:
    cleared: bool
    status: str  # cleared | audit_failed | decide_failed | repair_needed
    request_id: str
    session_id: str
    escalation_status: str
    message: str


class ClearFreezeService:
    """Hub-independent: writes audit via ExecutionIndexStore directly so the
    CLI (no hub), the MCP tool, and the chat path all share one primitive.
    """

    def clear_with_audit(
        self,
        project_root: Path,
        *,
        target_freeze: Any,
        reason: str,
        approver_label: str,
        source_kind: str,
        cleared_event_kind: str,
        approver_user_id: str | None = None,
        permission_name: str | None = None,
        kill_switch_bypass: bool = False,
        extra_payload: dict | None = None,
    ) -> ClearFreezeResult:
        request_id = str(getattr(target_freeze, "request_id", "") or "")
        session_id = str(getattr(target_freeze, "session_id", "") or "")
        freeze_kind = str(getattr(target_freeze, "kind", "") or "")
        fingerprint = str(getattr(target_freeze, "fingerprint_phrase", "") or "")
        base = {
            "request_id": request_id,
            "session_id": session_id,
            "freeze_kind": freeze_kind,
            "approver_label": approver_label,
            "approver_user_id": approver_user_id,
            "reason": reason,
            "kill_switch_bypass": kill_switch_bypass,
        }

        def _audit(
            kind: str,
            status: str,
            extra: dict | None = None,
            *,
            perm: str | None = None,
        ) -> bool:
            try:
                from .execution_index_store import ExecutionIndexStore

                kwargs: dict = {}
                if approver_user_id:
                    kwargs["user_id"] = approver_user_id
                if perm:
                    kwargs["permission_name"] = perm
                ExecutionIndexStore().record_event(
                    project_root,
                    event_kind=kind,
                    source_kind=source_kind,
                    session_id=session_id,
                    capability_name="clear_freeze",
                    action_kind="clear",
                    target_entity=request_id,
                    status=status,
                    payload={**base, **(extra or {})},
                    **kwargs,
                )
                return True
            except Exception:
                return False

        # 1. Durable intent ledger FIRST.
        if not _audit("freeze_clear_initiated", "initiated"):
            return ClearFreezeResult(
                False,
                "audit_failed",
                request_id,
                session_id,
                "no_change",
                "freeze clear could not be recorded (audit write failed); freeze KEPT.",
            )

        # 2. Decline the linked escalation.
        escalation_status = "no_change"
        try:
            from .escalation_store import EscalationStore

            decided = EscalationStore().decide(
                project_root,
                request_id,
                approve=False,
                approver_user_id=approver_user_id,
                approver_label=approver_label,
                reason=f"clear-freeze ({source_kind}): {reason}".strip(),
            )
            escalation_status = getattr(decided, "status", "no_change") if decided else "no_change"
        except Exception:
            _audit("freeze_clear_degraded", "degraded", {"stage": "escalation_decide"})
            return ClearFreezeResult(
                False,
                "decide_failed",
                request_id,
                session_id,
                "no_change",
                "escalation decline failed; freeze KEPT (request pending).",
            )

        # 3. Lift the lock.
        try:
            from .session_freeze_store import SessionFreezeStore

            cleared = SessionFreezeStore().clear_freeze(project_root, session_id)
        except Exception:
            _audit(
                "freeze_clear_repair_needed",
                "degraded",
                {
                    "note": "escalation declined but freeze still active",
                    "escalation_status": escalation_status,
                },
            )
            return ClearFreezeResult(
                False,
                "repair_needed",
                request_id,
                session_id,
                escalation_status,
                "escalation declined but lock not lifted — flagged for repair.",
            )

        # 4a. clear_freeze returned False (no exception, but ZERO rows
        #     deleted) → NOT cleared. Never herald a false success: emit a
        #     durable repair marker and report the truth.
        if not cleared:
            _audit(
                "freeze_clear_repair_needed",
                "degraded",
                {
                    "note": "clear_freeze returned false; no row deleted",
                    "escalation_status": escalation_status,
                },
            )
            return ClearFreezeResult(
                False,
                "not_cleared",
                request_id,
                session_id,
                escalation_status,
                "escalation declined but no freeze row was deleted -- flagged for repair.",
            )

        # 4b. STRIKE RESET (2026-06-11): the operator clearing a freeze is an
        # explicit trust decision — "we start fresh." Pre-fix the strike
        # counter (security_violation_service._count_strikes) was a raw
        # COUNT(*) with no reset marker, so clearing the lock left the count
        # intact and a stable session id ratcheted strikes across DAYS into an
        # instant re-freeze. Emit a session-scoped reset marker that
        # _count_strikes honours (counts only strikes observed AFTER it).
        # MUST be emitted BEFORE the cleared surface event below: the ledger
        # contract (test_clear_freeze_service.test_happy_path_ledger_first)
        # requires cleared_event_kind to be the LAST event in the chain.
        _audit(
            "security_violation_reset",
            "reset",
            {"note": "freeze cleared by operator/admin — strike count reset"},
        )

        # 4c. Cleared for real → surface-specific cleared event. TERMINAL
        # surface signal — must remain the last audit row in the chain.
        _audit(
            cleared_event_kind,
            "cleared",
            {
                "fingerprint_phrase": fingerprint,
                "escalation_status": escalation_status,
                **(extra_payload or {}),
            },
            perm=permission_name,
        )
        return ClearFreezeResult(
            True,
            "cleared",
            request_id,
            session_id,
            escalation_status,
            f"freeze cleared by {approver_label} (reason: {reason}).",
        )
