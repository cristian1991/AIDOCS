"""Audited, idempotent cleanup of false-positive update-intent rows.

Stored ``pending_durable_writes`` rows carry only the detected snippet
(``sentence[:SNIPPET_MAX]``). A row is a FALSE POSITIVE only when the cleanup
can PROVE it, i.e. the stored text is the COMPLETE sentence and it would not be
classified today by the same provenance floor + classifier the live hook uses:

  * ``non_operator_text`` - the floor (``strip_non_operator_text``) leaves no
    operator-authored text (peer / pasted / system / tool blocks, automation
    markers);
  * ``classifier_rejects`` - the operator text no longer classifies as an
    update intent (tag-name words, noun uses, past tense, negation, ...).

A snippet that may be TRUNCATED (length >= SNIPPET_MAX; the original sentence
could hold an instruction beyond the prefix) proves nothing: it is NEVER
auto-closed, it is reported under ``needs_review`` for a human.

Rows are NEVER deleted. A false positive moves to status ``false_positive``
(reason kept in ``satisfied_by``). Audit is FIRST and FAIL-CLOSED: the
``update_intent_false_positive`` event must persist before the row is marked;
if the audit write fails the row is left untouched, the run stops and
``ok`` is False. Dry run (the default) only lists. Re-running is a no-op:
only ``pending``/``expired`` rows are considered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import pending_durable_writes_store as _pdw
from .canonical_intent_registry import strip_non_operator_text
from .update_intent_detector import SNIPPET_MAX, detect_update_intent

__all__ = ["NEEDS_REVIEW", "classify_row", "cleanup_false_positives"]

_SCAN_STATUSES = ("pending", "expired")

# Sentinel: the stored text cannot prove the verdict either way.
NEEDS_REVIEW = "needs_review_possibly_truncated"


def classify_row(row: dict[str, Any]) -> str | None:
    """Return the false-positive reason for a stored row, ``NEEDS_REVIEW`` when
    the stored evidence is possibly truncated, or None to keep the row."""
    snippet = str(row.get("snippet") or "")
    if len(snippet) >= SNIPPET_MAX:
        return NEEDS_REVIEW
    operator_text = strip_non_operator_text(snippet)
    if not operator_text.strip():
        return "non_operator_text"
    if not detect_update_intent(operator_text).detected:
        return "classifier_rejects"
    return None


def _audit_strict(project_root: Path, session_id: str, payload: dict[str, Any]) -> str:
    """Persist one audit event; ANY failure propagates (no swallowing)."""
    from .execution_index_store import ExecutionIndexStore

    return ExecutionIndexStore().record_event(
        project_root,
        event_kind="update_intent_false_positive",
        source_kind="update_intent",
        session_id=session_id,
        capability_name="update_intent_gate",
        action_kind="durability",
        target_entity=str(payload.get("snippet") or "")[:200],
        status=str(payload.get("status") or "recorded"),
        payload=payload,
    )


def cleanup_false_positives(
    project_root: Path, *, apply: bool = False, note: str = ""
) -> dict[str, Any]:
    """Find (and with ``apply`` mark) false-positive rows. See module doc."""
    rows = _pdw.list_rows(project_root, _SCAN_STATUSES)
    candidates: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    for row in rows:
        reason = classify_row(row)
        if reason is None:
            continue
        entry = {
            "id": row["id"],
            "session_id": row["session_id"],
            "status": row["status"],
            "snippet": row["snippet"],
            "reason": reason,
        }
        (needs_review if reason == NEEDS_REVIEW else candidates).append(entry)
    marked: list[int] = []
    unmarked: list[int] = []
    error: str | None = None
    if apply:
        # Two phases per row (the mark and the audit live in different stores,
        # so one transaction is impossible): an ATTEMPTED event states the
        # intent BEFORE the mark; an OUTCOME event (applied / not_applied with
        # the observed status) is written AFTER it. No event ever claims a
        # false_positive status the row does not have.
        for cand in candidates:
            base = {
                "row_id": cand["id"],
                "previous_status": cand["status"],
                "reason": cand["reason"],
                "note": note,
                "snippet": cand["snippet"],
            }
            sid = str(cand["session_id"])
            try:
                _audit_strict(
                    project_root,
                    sid,
                    {**base, "phase": "attempted", "status": "attempted", "intent": "false_positive"},
                )
            except Exception as exc:  # noqa: BLE001 - fail closed, report
                error = f"audit_write_failed: row {cand['id']}: {type(exc).__name__}: {exc}"
                break  # row NOT marked; stop the run
            try:
                did_mark = _pdw.mark_false_positive(project_root, cand["id"], cand["reason"])
            except Exception as exc:  # noqa: BLE001 - report, stop
                error = f"mark_failed: row {cand['id']}: {type(exc).__name__}: {exc}"
                break
            observed = "false_positive" if did_mark else _pdw.get_status(project_root, cand["id"])
            try:
                _audit_strict(
                    project_root,
                    sid,
                    {
                        **base,
                        "phase": "applied" if did_mark else "not_applied",
                        "status": "applied" if did_mark else "not_applied",
                        "observed_status": observed,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - the mark happened; say so
                error = f"outcome_audit_failed: row {cand['id']}: {type(exc).__name__}: {exc}"
                if did_mark:
                    marked.append(cand["id"])
                else:
                    unmarked.append(cand["id"])
                break
            if did_mark:
                marked.append(cand["id"])
            else:
                unmarked.append(cand["id"])  # status changed under us
    if unmarked and error is None:
        # NO false green: a row we meant to close was not closed.
        error = f"rows_not_applied: {unmarked}"
    return {
        "ok": error is None,
        "error": error,
        "dry_run": not apply,
        "scanned": len(rows),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "needs_review_count": len(needs_review),
        "needs_review": needs_review,
        "marked": marked,
        "unmarked_after_audit": unmarked,
    }
