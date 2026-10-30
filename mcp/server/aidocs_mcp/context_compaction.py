"""Context compaction — summarize session history for context budget management.

Since AIDOCS is an MCP server (not a host), we can't directly manage the
conversation window. Instead we provide:

1. context_budget_check — estimates current context usage from the session's
   activity rows in the audit DB (execution_events)
2. compact_session_context — produces a summary of session work so far,
   suitable for injection after host-level compaction

#1223: there is no session journal file any more, so nothing here prunes or
rewrites anything; the audit ledger is governed by its own retention.
The ``journal_*`` field names are kept for the dashboard shape.

No LLM calls — uses deterministic extraction of key decisions, files touched,
and current state. The host's own compaction handles conversation history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ContextBudget:
    session_id: str
    journal_entries: int
    journal_chars: int
    context_files: int
    handoff_steps: int
    estimated_tokens: int
    over_budget: bool
    recommendation: str

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "journal_entries": self.journal_entries,
            "journal_chars": self.journal_chars,
            "context_files": self.context_files,
            "handoff_steps": self.handoff_steps,
            "estimated_tokens": self.estimated_tokens,
            "over_budget": self.over_budget,
            "recommendation": self.recommendation,
        }


@dataclass(slots=True)
class CompactedContext:
    session_id: str
    summary: str
    key_decisions: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)
    current_state: str = ""
    upcoming: list[str] = field(default_factory=list)
    pruned_entries: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "summary": self.summary,
            "key_decisions": self.key_decisions,
            "files_touched": self.files_touched,
            "current_state": self.current_state,
            "upcoming": self.upcoming,
            "pruned_entries": self.pruned_entries,
        }


# Token budget thresholds
_JOURNAL_TOKEN_WARNING = 5000
_JOURNAL_TOKEN_CRITICAL = 10000


def _session_activity(session_path: Path, session_id: str) -> list[dict[str, str]]:
    """The session's activity rows from execution_events.

    ``session_path`` is ``<project_root>/.MEMORY/sessions/<session_id>``.
    Best-effort: an unreadable ledger reads as no activity.
    """
    from .execution_index_store import ExecutionIndexStore

    project_root = session_path.parent.parent.parent
    try:
        return ExecutionIndexStore().session_activity(project_root, session_id)
    except Exception:
        return []


def _entry_text(entry: dict[str, str]) -> str:
    return " | ".join(
        str(entry.get(k) or "") for k in ("timestamp", "action_kind", "intent", "outcome")
    )


def check_context_budget(
    session_path: Path,
    session_id: str,
) -> ContextBudget:
    """Estimate context budget usage for a session.

    Checks session activity size, context file count, and handoff steps.
    Returns budget status with recommendation.
    """
    context_path = session_path / "context.md"
    handoff_path = session_path / f"{session_id}.handoff.md"

    activity = _session_activity(session_path, session_id)
    journal_entries = len(activity)
    journal_chars = sum(len(_entry_text(entry)) + 1 for entry in activity)

    context_files = 0
    if context_path.is_file():
        ctx_text = context_path.read_text(encoding="utf-8", errors="replace")
        context_files = ctx_text.count("- `") + ctx_text.count("- /")

    handoff_steps = 0
    if handoff_path.is_file():
        hoff_text = handoff_path.read_text(encoding="utf-8", errors="replace")
        handoff_steps = hoff_text.count("- [")

    estimated_tokens = journal_chars // 4

    if estimated_tokens > _JOURNAL_TOKEN_CRITICAL:
        recommendation = "Critical: session activity exceeds token budget. Use compact_session_context for a summary."
        over_budget = True
    elif estimated_tokens > _JOURNAL_TOKEN_WARNING:
        recommendation = "Warning: session activity approaching token budget. Consider compacting soon."
        over_budget = False
    else:
        recommendation = "Context budget is healthy."
        over_budget = False

    return ContextBudget(
        session_id=session_id,
        journal_entries=journal_entries,
        journal_chars=journal_chars,
        context_files=context_files,
        handoff_steps=handoff_steps,
        estimated_tokens=estimated_tokens,
        over_budget=over_budget,
        recommendation=recommendation,
    )


def compact_session_context(
    session_path: Path,
    session_id: str,
    *,
    keep_recent: int = 10,
) -> CompactedContext:
    """Compact session context by extracting key information.

    Reads session artifacts (SESSION.md, context.md) and the session's
    activity rows in execution_events, and produces a structured summary.
    Nothing is pruned (#1223): ``pruned_entries`` is always 0.

    Args:
        session_path: Path to the session directory.
        session_id: Session identifier.
        keep_recent: Kept for call compatibility; unused.

    Returns:
        CompactedContext with summary, decisions, files, state.

    """
    # Read session file
    session_file = session_path / "SESSION.md"
    session_text = ""
    if session_file.is_file():
        session_text = session_file.read_text(encoding="utf-8", errors="replace")

    # Extract state and upcoming from SESSION.md
    current_state = _extract_section(session_text, "State")
    upcoming = [
        line.strip("- ").strip()
        for line in _extract_section(session_text, "Upcoming").splitlines()
        if line.strip().startswith("-")
    ]

    # Read context.md for files
    context_file = session_path / "context.md"
    files_touched: list[str] = []
    if context_file.is_file():
        ctx_text = context_file.read_text(encoding="utf-8", errors="replace")
        files_section = _extract_section(ctx_text, "Relevant Files")
        files_touched = [
            line.strip("- ").strip().strip("`")
            for line in files_section.splitlines()
            if line.strip().startswith("- ") and line.strip() != "-"
        ]

    # Key decisions from the session's activity rows (execution_events).
    key_decisions: list[str] = []
    pruned_entries = 0
    for entry in _session_activity(session_path, session_id):
        text = _entry_text(entry)
        if any(
            kw in text.lower()
            for kw in (
                "decided",
                "chose",
                "switched",
                "blocked",
                "fixed",
                "resolved",
                "created",
                "implemented",
            )
        ):
            key_decisions.append(text[:200])

    # Build summary
    goal = _extract_section(session_text, "Goal").strip("- \n")
    title = _extract_section(session_text, "Title").strip("- \n")
    summary = f"Session: {title or session_id}. Goal: {goal or 'not set'}."
    if key_decisions:
        summary += f" {len(key_decisions)} key decisions recorded."
    if files_touched:
        summary += f" {len(files_touched)} files in scope."

    return CompactedContext(
        session_id=session_id,
        summary=summary,
        key_decisions=key_decisions[:20],  # cap at 20
        files_touched=files_touched,
        current_state=current_state.strip(),
        upcoming=upcoming,
        pruned_entries=pruned_entries,
    )


def _extract_section(text: str, heading: str) -> str:
    """Extract content under a ## heading."""
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = text.find("\n## ", start)
    if end < 0:
        return text[start:].strip()
    return text[start:end].strip()

