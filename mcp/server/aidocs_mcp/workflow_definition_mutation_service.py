"""Ledger-first mutation primitive for workflow_definitions (Stage 5c).

AIDOCS law: no control-plane mutation without durable audit. The
workflow_definition_* tools mutate the canonical workflow rule/action SQL
store, so each mutation runs in a fixed order:

    1. validate the intended mutation WITHOUT writing
    2. record a durable INTENT audit event before any SQL write
       (workflow_definition_{add,update,remove}_initiated)
    3. perform the SQL mutation
    4. recompile (failure surfaced, never hidden)
    5. record the FINAL audit event
       (workflow_definition_{added,updated,removed})

If the intent audit fails, NOTHING is mutated (truthful ok=false,
mutated=false). If the mutation succeeds but the final audit fails, the
result is reported as repair-needed — never clean success — and a
workflow_definition_audit_repair_needed event is attempted so the
mutated-but-unaudited state is visible. Audit payloads carry a body_hash,
never the full body.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import workflow_definitions_store as _wd

_KINDS = ("rule", "action")


def _body_hash(body: str | None) -> str | None:
    if body is None:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class WorkflowDefinitionMutationService:
    def __init__(self, audit_store: Any = None) -> None:
        # audit_store is injectable for tests; None resolves the default
        # ExecutionIndexStore lazily.
        self._audit_store = audit_store

    def _store(self):
        if self._audit_store is not None:
            return self._audit_store
        from .execution_index_store import ExecutionIndexStore

        return ExecutionIndexStore()

    def _record(
        self,
        project_root: Path,
        *,
        event_kind: str,
        status: str,
        payload: dict[str, Any],
    ) -> tuple[bool, str | None]:
        try:
            self._store().record_event(
                project_root,
                event_kind=event_kind,
                source_kind="workflow_definition_tool",
                capability_name=event_kind,
                action_kind="mutate",
                target_entity="workflow_definitions",
                status=status,
                payload=payload,
            )
            return True, None
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def _recompile_summary(
        recompile: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if recompile is None:
            return None
        return {
            "recompile_ok": recompile.get("recompile_ok"),
            "action_count": recompile.get("recompiled_action_count"),
            "unsupported_count": recompile.get("unsupported_count"),
            "definitions_source": recompile.get("definitions_source"),
        }

    @staticmethod
    def _safe_recompile(
        recompile_fn: Callable[[], dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        """Run the recompile callback inside the service so a raising callback
        can never strand a mutation without a final audit. A raise is
        converted to a degraded summary; the mutation still finalizes.
        """
        if recompile_fn is None:
            return None
        try:
            return recompile_fn()
        except Exception as exc:
            return {
                "recompile_ok": False,
                "recompile_error": str(exc),
                "recompiled_action_count": 0,
            }

    def _record_mutation_failed(
        self,
        project_root: Path,
        *,
        event_kind: str,
        def_id: Any,
        kind: str,
        approver: str,
        source_surface: str,
        reason: str | None,
    ) -> None:
        # Best-effort diagnostic so an initiated-without-final-success is easy
        # to spot. Never raises.
        self._record(
            project_root,
            event_kind=event_kind,
            status="failed",
            payload={
                "def_id": def_id,
                "kind": kind,
                "approver": approver,
                "source_surface": source_surface,
                "mutated": False,
                "reason": reason,
            },
        )

    def _finalize(
        self,
        project_root: Path,
        *,
        event_kind: str,
        def_id: Any,
        kind: str,
        status: str,
        approver: str,
        recompile: dict[str, Any] | None,
        source_surface: str,
        mutation_result: dict[str, Any] | None,
        extra_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        final_payload: dict[str, Any] = {
            "def_id": def_id,
            "kind": kind,
            "status": status,
            "approver": approver,
            "source_surface": source_surface,
        }
        if extra_payload:
            final_payload.update(extra_payload)
        summary = self._recompile_summary(recompile)
        if summary is not None:
            final_payload["recompile"] = summary

        ok, err = self._record(
            project_root,
            event_kind=event_kind,
            status=status,
            payload=final_payload,
        )
        if not ok:
            # The SQL mutation already landed. We must NOT report clean
            # success, but we MUST tell the caller the mutation happened
            # (mutated=True) and leave a durable repair marker.
            repair_ok, _ = self._record(
                project_root,
                event_kind="workflow_definition_audit_repair_needed",
                status="repair_needed",
                payload={
                    **final_payload,
                    "for_event": event_kind,
                    "final_audit_error": err,
                },
            )
            result: dict[str, Any] = {
                "ok": False,
                "status": "audit_repair_needed",
                "mutated": True,
                "id": def_id,
                "approver": approver,
                "final_audit_error": err,
                "repair_event_recorded": repair_ok,
            }
            if mutation_result:
                result.update(mutation_result)
            if recompile:
                result.update(recompile)
            return result

        result = {
            "ok": True,
            "status": "ok",
            "mutated": True,
            "id": def_id,
            "approver": approver,
        }
        if mutation_result:
            result.update(mutation_result)
        if recompile:
            result.update(recompile)
        return result

    def add(
        self,
        project_root: Path,
        *,
        kind: str,
        body: str,
        approver: str,
        recompile_fn: Callable[[], dict[str, Any]] | None = None,
        source_surface: str = "workflow_definition_tool",
    ) -> dict[str, Any]:
        if kind not in _KINDS:
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": f"kind {kind!r} not in {list(_KINDS)}",
            }
        body = (body or "").strip()
        if not body:
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": "body is required",
            }
        bh = _body_hash(body)
        ok, err = self._record(
            project_root,
            event_kind="workflow_definition_add_initiated",
            status="initiated",
            payload={
                "kind": kind,
                "status": "active",
                "approver": approver,
                "source_surface": source_surface,
                "body_hash": bh,
            },
        )
        if not ok:
            return {
                "ok": False,
                "blocked_by": "audit_intent_failed",
                "mutated": False,
                "reason": err,
            }
        res = _wd.add(project_root, kind=kind, body=body, source="dashboard")
        if not res.get("ok"):
            self._record_mutation_failed(
                project_root,
                event_kind="workflow_definition_mutation_failed",
                def_id=None,
                kind=kind,
                approver=approver,
                source_surface=source_surface,
                reason=res.get("error"),
            )
            return {**res, "mutated": False}
        recompile = self._safe_recompile(recompile_fn)
        return self._finalize(
            project_root,
            event_kind="workflow_definition_added",
            def_id=res.get("id"),
            kind=kind,
            status="active",
            approver=approver,
            recompile=recompile,
            source_surface=source_surface,
            mutation_result={"definition": res},
            extra_payload={"body_hash": bh},
        )

    def update(
        self,
        project_root: Path,
        *,
        def_id: int,
        body: str | None = None,
        status: str | None = None,
        approver: str,
        recompile_fn: Callable[[], dict[str, Any]] | None = None,
        source_surface: str = "workflow_definition_tool",
    ) -> dict[str, Any]:
        if status is not None and status == "removed":
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": "use remove() to tombstone; update cannot set status='removed'",
            }
        if body is None and status is None:
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": "nothing to update",
            }
        existing = _wd.get(project_root, int(def_id))
        if existing is None:
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": f"definition {int(def_id)} not found",
            }
        existing_kind = existing["kind"]
        previous_status = existing["status"]
        previous_body_hash = _body_hash(existing["body"])
        new_body_hash = _body_hash(body)
        intended_status = status or (previous_status or "active")
        attribution: dict[str, Any] = {
            "def_id": int(def_id),
            "kind": existing_kind,
            "previous_status": previous_status,
            "status": intended_status,
            "approver": approver,
            "source_surface": source_surface,
        }
        if previous_body_hash is not None:
            attribution["previous_body_hash"] = previous_body_hash
        if new_body_hash is not None:
            attribution["new_body_hash"] = new_body_hash
        ok, err = self._record(
            project_root,
            event_kind="workflow_definition_update_initiated",
            status="initiated",
            payload=attribution,
        )
        if not ok:
            return {
                "ok": False,
                "blocked_by": "audit_intent_failed",
                "mutated": False,
                "reason": err,
            }
        res = _wd.update(
            project_root,
            def_id=int(def_id),
            body=body,
            status=status,
        )
        if not res.get("ok"):
            self._record_mutation_failed(
                project_root,
                event_kind="workflow_definition_mutation_failed",
                def_id=int(def_id),
                kind=existing_kind,
                approver=approver,
                source_surface=source_surface,
                reason=res.get("error"),
            )
            return {**res, "mutated": False}
        recompile = self._safe_recompile(recompile_fn)
        extra = {
            k: v
            for k, v in attribution.items()
            if k in ("previous_status", "previous_body_hash", "new_body_hash")
        }
        return self._finalize(
            project_root,
            event_kind="workflow_definition_updated",
            def_id=int(def_id),
            kind=existing_kind,
            status=intended_status,
            approver=approver,
            recompile=recompile,
            source_surface=source_surface,
            mutation_result=None,
            extra_payload=extra,
        )

    def remove(
        self,
        project_root: Path,
        *,
        def_id: int,
        reason: str = "",
        approver: str,
        recompile_fn: Callable[[], dict[str, Any]] | None = None,
        source_surface: str = "workflow_definition_tool",
    ) -> dict[str, Any]:
        existing = _wd.get(project_root, int(def_id))
        if existing is None:
            return {
                "ok": False,
                "blocked_by": "validation",
                "mutated": False,
                "error": f"definition {int(def_id)} not found",
            }
        existing_kind = existing["kind"]
        previous_status = existing["status"]
        previous_body_hash = _body_hash(existing["body"])
        attribution: dict[str, Any] = {
            "def_id": int(def_id),
            "kind": existing_kind,
            "previous_status": previous_status,
            "status": "removed",
            "approver": approver,
            "source_surface": source_surface,
            "reason": reason,
        }
        if previous_body_hash is not None:
            attribution["previous_body_hash"] = previous_body_hash
        ok, err = self._record(
            project_root,
            event_kind="workflow_definition_remove_initiated",
            status="initiated",
            payload=attribution,
        )
        if not ok:
            return {
                "ok": False,
                "blocked_by": "audit_intent_failed",
                "mutated": False,
                "reason": err,
            }
        res = _wd.remove(project_root, def_id=int(def_id), reason=reason)
        if not res.get("ok"):
            self._record_mutation_failed(
                project_root,
                event_kind="workflow_definition_mutation_failed",
                def_id=int(def_id),
                kind=existing_kind,
                approver=approver,
                source_surface=source_surface,
                reason=res.get("error"),
            )
            return {**res, "mutated": False}
        recompile = self._safe_recompile(recompile_fn)
        extra = {
            k: v
            for k, v in attribution.items()
            if k in ("previous_status", "previous_body_hash", "reason")
        }
        return self._finalize(
            project_root,
            event_kind="workflow_definition_removed",
            def_id=int(def_id),
            kind=existing_kind,
            status="removed",
            approver=approver,
            recompile=recompile,
            source_surface=source_surface,
            mutation_result=None,
            extra_payload=extra,
        )
