"""Column projection + audit-field hiding for list-mode MCP tools.

Backlog #14: agents picking next-work only need {id, content, priority,
status, tags}. Agents generating reports want everything including
audit FKs. Today every list returns the full dict — 40% token waste
on the common case. Let caller opt into a narrower projection while
preserving the report-generation use case.

Backlog #15 discussion (2026-04-25): also enforce RBAC at this layer.
Audit fields (created_in_session_id, source_task_id, linked_task_id,
promoted_from_todo_id, timestamps, machine_id) require auditor/admin
role; non-auditor callers get them stripped by default.

Contract:
  project_items(items, columns=None, caller_role=None)
    columns=None, caller is auditor-class → full record
    columns=None, caller is NOT auditor-class → audit fields stripped
    columns=[x,y,z] → exact projection; AUDIT fields refused unless caller has role
    columns=["*"] → full record; audit fields refused unless caller has role
"""

from __future__ import annotations

from typing import Any

# Fields considered "audit chrome" — admin/forensic metadata, not agent-
# actionable workflow data. Agent-useful fields (linked_task_id,
# promoted_from_todo_id, source_task_id) stay in the default view because
# they carry workflow context (see 2026-04-25 operator clarification:
# "audit chrome" = removed_at/removed_reason/machine_id/nulls only;
# workflow-context FKs stay visible).
AUDIT_CHROME_FIELDS: frozenset[str] = frozenset(
    {
        "removed_at",
        "removed_reason",
        "machine_id",
        "updated_at",
        "completed_at",
        "created_in_session_id",
    },
)

# Roles allowed to see audit chrome unredacted.
AUDITOR_ROLES: frozenset[str] = frozenset(
    {
        "auditor",
        "admin",
        "super_admin",
    },
)


def _is_auditor(role: str | None) -> bool:
    return bool(role) and role.strip().lower() in AUDITOR_ROLES


def project_items(
    items: list[dict[str, Any]],
    *,
    columns: list[str] | None = None,
    caller_role: str | None = None,
) -> list[dict[str, Any]]:
    """Apply RBAC-aware column projection to a list of records.

    Behavior matrix:
      columns=None + auditor → return items as-is
      columns=None + non-auditor → strip AUDIT_CHROME_FIELDS
      columns=[...] explicit → return exactly those keys (RBAC check
        per-column below)
      columns=["*"] → equivalent to columns=None + explicit audit opt-in
        (non-auditor gets audit stripped from wildcard too)

    RBAC enforcement for explicit columns: if caller is non-auditor and
    an audit field is requested explicitly, it's silently dropped from
    the projection (log-worthy, but doesn't error so the rest of the
    projection still works — less helpful to block the whole call).
    """
    if not items:
        return []

    auditor = _is_auditor(caller_role)

    if columns is None:
        if auditor:
            return list(items)
        return [_strip_audit(r) for r in items]

    if columns == ["*"]:
        if auditor:
            return list(items)
        return [_strip_audit(r) for r in items]

    allowed_cols = _filter_requested_cols(columns, auditor)
    return [{k: r[k] for k in allowed_cols if k in r} for r in items]


def _strip_audit(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in AUDIT_CHROME_FIELDS}


def _filter_requested_cols(
    columns: list[str],
    auditor: bool,
) -> list[str]:
    normalized = [str(c).strip() for c in columns if str(c).strip()]
    if auditor:
        return normalized
    return [c for c in normalized if c not in AUDIT_CHROME_FIELDS]
