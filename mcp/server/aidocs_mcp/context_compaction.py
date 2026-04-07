"""Context compaction — summarize session history for context budget management.

Since AIDOCS is an MCP server (not a host), we can't directly manage the
conversation window. Instead we provide:

1. context_budget_check — estimates current context usage from session journal
2. compact_session_context — produces a summary of session work so far,
   suitable for injection after host-level compaction
3. Session journal pruning — archives old entries to keep journal lean

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


def check_context_budget(
    session_path: Path,
    session_id: str,
) -> ContextBudget:
    """Estimate context budget usage for a session.

    Checks journal size, context file count, and handoff steps.
    Returns budget status with recommendation.
    """
    journal_path = session_path / "journal.md"
    context_path = session_path / "context.md"
    handoff_path = session_path / f"{session_id}.handoff.md"

    journal_chars = 0
    journal_entries = 0
    if journal_path.is_file():
        text = journal_path.read_text(encoding="utf-8", errors="replace")
        journal_chars = len(text)
        journal_entries = text.count("\n## ")

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
        recommendation = "Critical: journal exceeds token budget. Run compact_session_context to prune old entries."
        over_budget = True
    elif estimated_tokens > _JOURNAL_TOKEN_WARNING:
        recommendation = "Warning: journal approaching token budget. Consider compacting soon."
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
    """Compact session context by extracting key information and pruning old journal entries.

    Reads session artifacts (SESSION.md, context.md, handoff, journal) and produces
    a structured summary. Optionally prunes journal entries beyond keep_recent.

    Args:
        session_path: Path to the session directory.
        session_id: Session identifier.
        keep_recent: Number of recent journal entries to keep (default 10).

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
    upcoming = [line.strip("- ").strip() for line in _extract_section(session_text, "Upcoming").splitlines() if line.strip().startswith("-")]

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

    # Read and prune journal
    journal_path = session_path / "journal.md"
    key_decisions: list[str] = []
    pruned_entries = 0

    if journal_path.is_file():
        journal_text = journal_path.read_text(encoding="utf-8", errors="replace")
        entries = _split_journal_entries(journal_text)

        # Extract key decisions from all entries
        for entry in entries:
            for line in entry.splitlines():
                stripped = line.strip()
                if any(kw in stripped.lower() for kw in ("decided", "chose", "switched", "blocked", "fixed", "resolved", "created", "implemented")):
                    key_decisions.append(stripped[:200])

        # Prune: keep header + recent entries
        if len(entries) > keep_recent:
            pruned_entries = len(entries) - keep_recent
            kept = entries[-keep_recent:]
            header = journal_text.split("\n## ")[0] if "\n## " in journal_text else "# Journal\n"
            pruned_text = header + "\n" + "\n".join(kept)
            journal_path.write_text(pruned_text, encoding="utf-8")

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


def _split_journal_entries(text: str) -> list[str]:
    """Split journal text into individual entries (## delimited)."""
    parts = text.split("\n## ")
    if len(parts) <= 1:
        return []
    return ["## " + part for part in parts[1:]]
