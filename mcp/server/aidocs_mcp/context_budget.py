from __future__ import annotations

from pathlib import Path
from typing import Any

from .execution_index_store import ExecutionIndexStore
from .session_store import SessionStore


def _estimate_tokens_from_text(text: str) -> int:
    return max(0, len(text) // 4)


def context_budget_check(project_root: Path, session_id: str | None) -> dict[str, Any]:
    if not session_id or not session_id.strip():
        return {
            "available": False,
            "reason": "No session selected.",
            "session_id": None,
        }

    session_id = session_id.strip()
    templates_root = project_root / ".MEMORY" / ".aidocs"
    session_store = SessionStore(templates_root=templates_root)
    execution_store = ExecutionIndexStore()

    session = session_store.read_session(project_root, session_id)
    context = session_store.read_context(project_root, session_id)
    plan = session_store.read_plan(project_root, session_id)
    handoff = session_store.read_handoff_optional(project_root, session_id)
    journal_entries = session_store.read_journal(project_root, session_id)

    section_sources = [session.sections, context.sections, plan.sections]
    if handoff is not None:
        section_sources.append(handoff.sections)
    combined_text = "\n".join(
        line for sections in section_sources for lines in sections.values() for line in lines
    )
    journal_text = "\n".join(
        f"{entry.get('timestamp', '')} {entry.get('intent', '')} {entry.get('outcome', '')}".strip()
        for entry in journal_entries
    )

    artifact_paths = session_store.session_code_targets(project_root, session_id)
    code_path_count = len(artifact_paths)

    session_tokens = 0
    token_rows = execution_store.query_token_breakdown_by_session(project_root)
    for row in token_rows:
        if str(row.get("session_id") or "") == session_id:
            session_tokens = int(row.get("total") or 0)
            break

    section_tokens = _estimate_tokens_from_text(combined_text)
    journal_tokens = _estimate_tokens_from_text(journal_text)
    path_tokens = code_path_count * 30
    estimated_tokens = section_tokens + journal_tokens + path_tokens + session_tokens

    # Thresholds scaled for Opus 4.7's 1M context window. The earlier
    # 12k/18k values were tuned for 200k-context models and tripped
    # "critical" at 10% usage on 1M models, spamming the dashboard.
    # TODO: make this percentage-based on the live model's context limit
    # once the runtime carries that metadata through to the dashboard.
    warning = estimated_tokens >= 650_000
    critical = estimated_tokens >= 900_000

    return {
        "available": True,
        "session_id": session_id,
        "journal_entries": len(journal_entries),
        "journal_tokens": journal_tokens,
        "section_tokens": section_tokens,
        "path_tokens": path_tokens,
        "execution_tokens": session_tokens,
        "estimated_tokens": estimated_tokens,
        "warning": warning,
        "critical": critical,
        "status": "critical" if critical else "warning" if warning else "ok",
    }


def context_compact(
    project_root: Path,
    session_id: str | None,
    keep_last: int = 20,
) -> dict[str, Any]:
    if not session_id or not session_id.strip():
        return {
            "ok": False,
            "reason": "No session selected.",
            "session_id": None,
        }

    session_id = session_id.strip()
    templates_root = project_root / ".MEMORY" / ".aidocs"
    session_store = SessionStore(templates_root=templates_root)
    journal_path = session_store.journal_path(project_root, session_id)
    if not journal_path.is_file():
        return {
            "ok": True,
            "session_id": session_id,
            "compacted": 0,
            "remaining": 0,
            "reason": "No journal file present.",
        }

    lines = [line for line in journal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) <= keep_last:
        return {
            "ok": True,
            "session_id": session_id,
            "compacted": 0,
            "remaining": len(lines),
            "reason": "Already within budget.",
        }

    evicted = session_store._evict_journal_entries(project_root, session_id, lines)
    return {
        "ok": True,
        "session_id": session_id,
        "compacted": evicted,
        "remaining": max(0, len(lines) - evicted),
        "reason": "Journal compacted.",
    }
