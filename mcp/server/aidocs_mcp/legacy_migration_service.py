from __future__ import annotations

from datetime import datetime
from pathlib import Path


class LegacyMigrationService:
    """Non-destructive helpers for inspecting and proposing legacy runtime migration."""

    def inspect_legacy(self, project_root: Path) -> dict[str, object]:
        memory_root = project_root / ".MEMORY"
        now_path = memory_root / "NOW.md"
        plans_root = memory_root / "plans"
        todo_path = memory_root / "TODO.md"
        done_path = memory_root / "DONE.md"

        plan_files = []
        if plans_root.is_dir():
            plan_files = [path.relative_to(memory_root).as_posix() for path in sorted(plans_root.rglob("*.md"))]

        agents_root = memory_root / "agents"
        agent_files = []
        if agents_root.is_dir():
            agent_files = [path.relative_to(memory_root).as_posix() for path in sorted(agents_root.rglob("*.md"))]

        result = {
            "has_now": now_path.is_file(),
            "has_todo": todo_path.is_file(),
            "has_done": done_path.is_file(),
            "has_root_plans": plans_root.is_dir(),
            "has_root_agents": agents_root.is_dir(),
            "plan_files": plan_files,
            "agent_files": agent_files,
            "now_summary": self._summarize_now(now_path) if now_path.is_file() else None,
        }
        result["legacy_present"] = any(
            [
                result["has_now"],
                result["has_todo"],
                result["has_done"],
                result["has_root_plans"],
                result["has_root_agents"],
            ]
        )
        return result

    def build_session_proposal(self, project_root: Path, session_id: str | None = None) -> dict[str, object]:
        legacy = self.inspect_legacy(project_root)
        if not legacy["has_now"] and not legacy["has_root_plans"]:
            return {
                "legacy_present": False,
                "message": "No legacy NOW.md or root plans found.",
                "next_step": "continue_with_session_model",
            }

        summary = legacy["now_summary"] or {}
        inferred_title = summary.get("goal") or summary.get("active") or "Legacy Session"
        inferred_goal = summary.get("goal") or summary.get("active") or "Continue migrated legacy task stream"
        inferred_scope = summary.get("state") or "Derived from legacy runtime state"
        session_id = session_id or self._default_session_id(inferred_title)

        return {
            "legacy_present": True,
            "proposed_session_id": session_id,
            "title": inferred_title,
            "goal": inferred_goal,
            "scope": inferred_scope,
            "upcoming": summary.get("upcoming_items", []),
            "blockers": summary.get("blockers", []),
            "plan_files": legacy["plan_files"],
            "decision_required": True,
            "next_step": "issue_stop_and_ask_for_migration_path",
            "options": [
                {
                    "id": "clean_session",
                    "label": "Create a new clean session",
                    "description": "Ignore legacy runtime details and start with a fresh session scaffold.",
                },
                {
                    "id": "migrate_legacy",
                    "label": "Create a session from legacy runtime",
                    "description": "Use the proposal below to create a session from the current legacy runtime and plans.",
                },
            ],
        }

    def _summarize_now(self, now_path: Path) -> dict[str, object]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for raw_line in now_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                current = line[3:].strip().lower()
                sections[current] = []
                continue
            if current is not None and line.strip().startswith("-"):
                sections[current].append(line.strip()[1:].strip())

        return {
            "goal": self._first_item(sections.get("goal", [])),
            "active": self._first_item(sections.get("active", [])),
            "state": self._first_item(sections.get("state", [])),
            "upcoming_items": sections.get("upcoming", []),
            "blockers": sections.get("blockers", []),
        }

    def _first_item(self, values: list[str]) -> str | None:
        return values[0] if values else None

    def _default_session_id(self, title: str) -> str:
        slug = "-".join(
            part for part in "".join(ch.lower() if ch.isalnum() else "-" for ch in title).split("-") if part
        ) or "legacy-session"
        return f"{datetime.now().strftime('%Y-%m-%d')}-{slug}"
