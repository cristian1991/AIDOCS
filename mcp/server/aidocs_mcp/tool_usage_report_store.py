"""Tool-usage feedback store — War HH (Emperor charter 2026-07-19).

``ai_task(mode='complete')`` accepts an optional ``tool_report``
parameter: [{tool: str, rating: int 1-10, note: str}] — the structured
version of the mandatory tool-feedback section wars carry in their
REPORTS. This module owns the whole feature's substrate:

1. SHAPE (``validate_tool_report``): strict schema validation with a
   legible refusal string naming the expected schema (War R/AZ
   refusal-envelope precedent). Garbage never reaches the store.
2. AUDIENCE (``caller_tool_report_authority``): the param is honored
   ONLY for superadmin/dev principals, resolved from the AUTHENTICATED
   principal via project_authority + the RBAC store (#449 role-branch
   precedent). distribution.flavor is NOT authority (#404). Fail-closed:
   unresolvable callers are not privileged.
3. STORAGE (``ToolUsageReportStore`` / ``record_tool_reports``): durable
   ``tool_usage_reports`` rows in the kingdom sqlite (sibling of
   session_response_ledger; census-classified in canonical_taxonomy as
   feedback telemetry, not doctrine rows).
4. PIPELINE: ONE compact digest line per task_complete appended to
   backlog #469 (the tool-feedback ledger) as a reason-only annotation
   (#314) — batched, mechanical harvest. Ranking into #183 remains a
   conductor act; this module never ranks.

Feedback is NEVER load-bearing: every write path here is fail-quiet
with a logged warning — a broken feedback store must not fail a
task_complete.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from ._sqlite_index_store_base import SQLiteIndexStoreBase

__all__ = [
    "EXPECTED_SCHEMA",
    "NUDGE_TEXT",
    "RULE_ID_AUDIENCE",
    "RULE_ID_SCHEMA",
    "TOOL_FEEDBACK_BACKLOG_ID",
    "TOOL_REPORT_PRIVILEGED_ROLES",
    "ToolUsageReportStore",
    "caller_tool_report_authority",
    "record_tool_reports",
    "validate_tool_report",
]

logger = logging.getLogger("aidocs.tool_usage_report")

# RBAC role names honored for tool_report (#449 role-branch precedent —
# note: deliberately NARROWER than tool_gate_service._ADMIN_HINT_ROLES;
# the charter names superadmin and dev accounts, not plain admin).
TOOL_REPORT_PRIVILEGED_ROLES: frozenset[str] = frozenset({"super_admin", "superadmin", "dev"})

# The tool-feedback ledger backlog item (harvested by the conductor;
# ranked aggregation into #183 is a conductor/War-U act, never automatic).
TOOL_FEEDBACK_BACKLOG_ID = 469

RULE_ID_SCHEMA = "tool_report.schema"
RULE_ID_AUDIENCE = "tool_report.audience"

_ALLOWED_KEYS = frozenset({"tool", "rating", "note"})
_TOOL_MAX_CHARS = 200
_NOTE_MAX_CHARS = 2000
_MAX_REPORTS_PER_CALL = 50
_DIGEST_NOTE_SNIPPET = 120

EXPECTED_SCHEMA = (
    "expected schema: tool_report = list of "
    "{tool: str (non-empty, <=200 chars), rating: int 1-10, "
    "note: str (<=2000 chars, the one concrete improvement; optional)}"
)

# One line, gentle, never blocking — surfaced once per session via the
# response ledger's notify-on-change dedupe.
NUDGE_TEXT = (
    "Optional: ai_task(mode='complete') accepts "
    "tool_report=[{tool, rating 1-10, note}] — one line of tool feedback "
    "per task feeds the #469 ledger."
)


def validate_tool_report(raw: Any) -> tuple[list[dict[str, Any]], str]:
    """Validate + normalize a tool_report payload.

    Returns ``(cleaned, "")`` on success or ``([], error)`` where
    ``error`` is a legible refusal naming the expected schema and the
    specific violation.
    """

    def _refuse(problem: str) -> tuple[list[dict[str, Any]], str]:
        return [], f"tool_report refused: {problem}. {EXPECTED_SCHEMA}"

    if not isinstance(raw, list):
        return _refuse(f"payload must be a list, got {type(raw).__name__}")
    if len(raw) > _MAX_REPORTS_PER_CALL:
        return _refuse(f"{len(raw)} entries exceeds the {_MAX_REPORTS_PER_CALL}-entry cap")
    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        where = f"entry [{i}]"
        if not isinstance(item, dict):
            return _refuse(f"{where} must be a dict, got {type(item).__name__}")
        unknown = sorted(set(item) - _ALLOWED_KEYS)
        if unknown:
            return _refuse(f"{where} carries unknown key(s) {unknown}")
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return _refuse(f"{where} is missing a non-empty 'tool' string")
        tool = tool.strip()
        if len(tool) > _TOOL_MAX_CHARS:
            return _refuse(f"{where} 'tool' exceeds {_TOOL_MAX_CHARS} chars")
        rating = item.get("rating")
        # bool is an int subclass — True/False are not ratings.
        if isinstance(rating, bool) or not isinstance(rating, int):
            return _refuse(f"{where} 'rating' must be an int 1-10")
        if not 1 <= rating <= 10:
            return _refuse(f"{where} 'rating' {rating} out of range — must be an int 1-10")
        note = item.get("note", "")
        if not isinstance(note, str):
            return _refuse(f"{where} 'note' must be a string when present")
        if len(note) > _NOTE_MAX_CHARS:
            return _refuse(
                f"{where} 'note' is {len(note)} chars — exceeds the {_NOTE_MAX_CHARS}-char cap"
            )
        cleaned.append({"tool": tool, "rating": rating, "note": note})
    return cleaned, ""


def caller_tool_report_authority(project_root: Path | str) -> tuple[bool, str]:
    """``(privileged, principal_uid)`` for the AUTHENTICATED caller.

    Mirrors tool_gate_service._resolve_caller_role_names (#449):
    project_authority's authenticated-uid resolution (dashboard token /
    machine login / approved host binding — never audit attribution),
    then the RBAC store's user→roles mapping. Fail-closed: any error or
    unauthenticated caller resolves to ``(False, "")``.
    """
    try:
        root = Path(project_root)
        from . import project_authority

        uid = project_authority._authenticated_uid(root)
        if not uid:
            return False, ""
        from .rbac_store import RBACStore

        roles = tuple(
            str(name).strip().lower() for name in RBACStore().get_user_permissions(root, uid).roles
        )
        return any(r in TOOL_REPORT_PRIVILEGED_ROLES for r in roles), str(uid)
    except Exception:
        return False, ""


class ToolUsageReportStore(SQLiteIndexStoreBase):
    """Durable ``tool_usage_reports`` rows in the kingdom sqlite."""

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_usage_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                task_id TEXT NOT NULL DEFAULT '',
                principal TEXT NOT NULL DEFAULT '',
                tool TEXT NOT NULL,
                rating INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """,
        )

    def add_reports(
        self,
        project_root: Path,
        *,
        session_id: str,
        task_id: str,
        principal: str,
        reports: list[dict[str, Any]],
    ) -> int:
        if not reports:
            return 0
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            now = self._timestamp()
            conn.executemany(
                "INSERT INTO tool_usage_reports "
                "(session_id, task_id, principal, tool, rating, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        session_id,
                        task_id,
                        principal,
                        str(r["tool"]),
                        int(r["rating"]),
                        str(r.get("note", "")),
                        now,
                    )
                    for r in reports
                ],
            )
        return len(reports)

    def list_reports(
        self,
        project_root: Path,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self.db_path(project_root)
        if not db.is_file():
            return []
        with self.session(project_root) as conn:
            self._ensure_schema(conn)
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM tool_usage_reports WHERE session_id = ? ORDER BY id",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tool_usage_reports ORDER BY id",
                ).fetchall()
        return [dict(r) for r in rows]


_STORE = ToolUsageReportStore()


def _build_digest(
    session_id: str,
    task_id: str,
    principal: str,
    reports: list[dict[str, Any]],
) -> str:
    """ONE compact, single-line digest per task_complete (batched)."""
    parts = []
    for r in reports:
        note = str(r.get("note", "")).replace("\n", " ").strip()
        if len(note) > _DIGEST_NOTE_SNIPPET:
            note = note[: _DIGEST_NOTE_SNIPPET - 1] + "…"
        chunk = f"{r['tool']}={r['rating']}"
        if note:
            chunk += f" ({note})"
        parts.append(chunk)
    return (
        f"tool_report session={session_id} task={task_id or '-'} "
        f"by={principal or '-'}: " + "; ".join(parts)
    )


def record_tool_reports(
    project_root: Path,
    *,
    session_id: str,
    task_id: str,
    principal: str,
    reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail-quiet ingest: durable rows + one #469 digest annotation.

    NEVER raises — feedback is not load-bearing. Returns a small
    receipt {stored, digest_appended} for the completion envelope.
    """
    out: dict[str, Any] = {"stored": 0, "digest_appended": False}
    if not reports:
        return out
    try:
        out["stored"] = _STORE.add_reports(
            project_root,
            session_id=session_id,
            task_id=task_id,
            principal=principal,
            reports=reports,
        )
    except Exception as exc:
        logger.warning("tool_usage_reports store failed (feedback dropped, non-fatal): %s", exc)
        return out
    try:
        from . import project_backlog_store

        digest = _build_digest(session_id, task_id, principal, reports)
        res = project_backlog_store.update(
            Path(project_root),
            backlog_id=TOOL_FEEDBACK_BACKLOG_ID,
            reason=digest,
        )
        out["digest_appended"] = bool(res.get("ok"))
        if not res.get("ok"):
            logger.warning(
                "tool_report digest append to backlog #%s refused (non-fatal): %s",
                TOOL_FEEDBACK_BACKLOG_ID,
                res.get("error"),
            )
    except Exception as exc:
        logger.warning("tool_report digest append failed (non-fatal): %s", exc)
    return out
