from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .constants import (
    CONTEXT_TEMPLATE_NAME,
    CONTEXT_SECTION_ORDER,
    HANDOFF_FILE_SUFFIX,
    HANDOFF_SECTION_ORDER,
    PLAN_SECTION_ORDER,
    PLAN_TEMPLATE_NAME,
    SESSION_SECTION_ORDER,
    SESSION_TEMPLATE_NAME,
    VALID_SESSION_STATUSES,
)
from .types import ContextData, HandoffData, PlanData, SessionData, SessionSummary


class SessionStore:
    """File-backed session discovery and mutation."""

    HANDOFF_STEP_MARKERS = {
        "open": "[ ]",
        "done": "[x]",
        "failed": "[!]",
        "reset": "[~]",
        "stale": "[?]",
    }

    def __init__(self, templates_root: Path) -> None:
        self.templates_root = templates_root

    def sessions_root(self, project_root: Path) -> Path:
        return project_root / ".MEMORY" / "sessions"

    def session_path(self, project_root: Path, session_id: str) -> Path:
        return self.sessions_root(project_root) / session_id

    def session_file(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / SESSION_TEMPLATE_NAME

    def context_file(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / CONTEXT_TEMPLATE_NAME

    def plan_file(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / "plans" / PLAN_TEMPLATE_NAME

    def handoff_file(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / f"{session_id}{HANDOFF_FILE_SUFFIX}"

    def _handoff_source_file(self, project_root: Path, session_id: str) -> Path | None:
        base = self.session_path(project_root, session_id)
        canonical = self.handoff_file(project_root, session_id)
        if canonical.exists():
            return canonical

        temp_candidates = sorted(
            (path for path in base.glob("*.TEMP") if path.is_file()),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
        if temp_candidates:
            return temp_candidates[0]
        return None

    def _default_handoff_sections(self, goal: str, created_at: str) -> dict[str, list[str]]:
        return {
            "Purpose": [f"- Handoff summary for the session goal: {goal.strip()}"],
            "Current State": ["- Session created; no implementation handoff has been recorded yet."],
            "What Was Done": ["- Session scaffold created."],
            "What Failed / Dead Ends": ["-"],
            "What Matters Now": ["- Convert this into a meaningful handoff once real work begins."],
            "Open Questions": ["-"],
            "Risks and Blockers": ["-"],
            "Relevant Files": ["-"],
            "Estimated Effort": ["-"],
            "Suggested Next Steps": ["- Start with a complete detailed plan and update the handoff as work progresses."],
            "Steps": ["-"],
            "Related Sessions": ["-"],
            "Related Project Links": ["-"],
            "Freshness": [f"- Created {created_at}; treat as fresh until implementation diverges."],
        }

    def _ensure_handoff(self, project_root: Path, session_id: str) -> None:
        canonical = self.handoff_file(project_root, session_id)
        if canonical.exists():
            return
        source = self._handoff_source_file(project_root, session_id)
        if source is not None and source.exists():
            canonical.parent.mkdir(parents=True, exist_ok=True)
            canonical.write_text(source.read_text(encoding="utf-8", errors="ignore"), encoding="utf-8")
            return
        session = self.read_session(project_root, session_id)
        goal = self._first_value(session, "Goal") or "Unknown session goal"
        created_at = self._timestamp()
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(self._render_handoff_sections(self._default_handoff_sections(goal, created_at)), encoding="utf-8")

    def read_handoff(self, project_root: Path, session_id: str) -> HandoffData:
        canonical = self.handoff_file(project_root, session_id)
        if not canonical.exists():
            self._ensure_handoff(project_root, session_id)
        path = canonical
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in HANDOFF_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        return HandoffData(session_id=session_id, path=path, sections=sections)

    def update_handoff(self, project_root: Path, session_id: str, patch: dict[str, list[str]], append: bool = False) -> HandoffData:
        data = self.read_handoff(project_root, session_id)
        for key, value in patch.items():
            if key not in HANDOFF_SECTION_ORDER:
                raise ValueError(f"Unknown handoff section: {key}")
            if append:
                existing = [item for item in data.sections.get(key, ["-"]) if item.strip() and item.strip() != "-"]
                incoming = [item for item in (value or ["-"]) if item.strip() and item.strip() != "-"]
                data.sections[key] = existing + incoming or ["-"]
            else:
                data.sections[key] = value or ["-"]
        if "Freshness" not in patch:
            data.sections["Freshness"] = [f"- Updated {self._timestamp()} automatically."]
        self.handoff_file(project_root, session_id).write_text(self._render_handoff_sections(data.sections), encoding="utf-8")
        return self.read_handoff(project_root, session_id)

    def read_handoff_steps(self, project_root: Path, session_id: str) -> list[dict[str, str]]:
        handoff = self.read_handoff(project_root, session_id)
        return self._parse_handoff_steps(handoff.sections.get("Steps", []))

    def upsert_handoff_step(
        self,
        project_root: Path,
        session_id: str,
        step_id: str | None = None,
        text: str | None = None,
        status: str = "open",
        append: bool = True,
    ) -> HandoffData:
        if status not in self.HANDOFF_STEP_MARKERS:
            raise ValueError(f"Unknown handoff step status: {status}")
        existing = self.read_handoff_steps(project_root, session_id)
        now = self._timestamp()
        if step_id is None:
            max_id = 0
            for item in existing:
                raw = str(item.get("id") or "")
                if raw.startswith("s") and raw[1:].isdigit():
                    max_id = max(max_id, int(raw[1:]))
            step_id = f"s{max_id + 1}"
        updated = []
        found = False
        for item in existing:
            if item["id"] == step_id:
                found = True
                updated.append(
                    {
                        "id": step_id,
                        "status": status or item["status"],
                        "text": text if text is not None else item["text"],
                        "updated_at": now,
                    }
                )
            else:
                updated.append(item)
        if not found:
            if not append and existing:
                updated = []
            updated.append({"id": step_id, "status": status, "text": text or step_id, "updated_at": now})
        step_lines = self._render_handoff_steps(updated)
        return self.update_handoff(project_root, session_id, {"Steps": step_lines}, append=False)

    def read_plan(self, project_root: Path, session_id: str) -> PlanData:
        path = self.plan_file(project_root, session_id)
        if not path.exists():
            raise FileNotFoundError(path)
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in PLAN_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        return PlanData(session_id=session_id, path=path, sections=sections)

    def update_plan(self, project_root: Path, session_id: str, patch: dict[str, list[str]]) -> PlanData:
        data = self.read_plan(project_root, session_id)
        for key, value in patch.items():
            if key not in PLAN_SECTION_ORDER:
                raise ValueError(f"Unknown plan section: {key}")
            data.sections[key] = value or ["-"]
        self.plan_file(project_root, session_id).write_text(self._render_plan_sections(data.sections), encoding="utf-8")
        return self.read_plan(project_root, session_id)

    def session_code_targets(self, project_root: Path, session_id: str) -> list[str]:
        session = self.read_session(project_root, session_id)
        context = self.read_context(project_root, session_id)
        targets: list[str] = []

        # direct context relevant files
        for line in context.sections.get("Relevant Files", []):
            candidate = self._extract_bullet_path(line)
            if candidate and self._looks_like_code_path(candidate):
                targets.append(candidate)

        # session-local plans can mention code files even when context is still broad
        plans_dir = self.session_path(project_root, session_id) / "plans"
        if plans_dir.is_dir():
            for plan_file in sorted(plans_dir.glob("*.md")):
                targets.extend(self._extract_code_paths_from_text(plan_file.read_text(encoding="utf-8", errors="ignore")))

        # key memory links inside SESSION.md may point to plan files within the session
        for line in session.sections.get("Key Memory Links", []):
            linked = self._extract_bullet_path(line)
            if not linked:
                continue
            if linked.startswith("plans/"):
                plan_path = self.session_path(project_root, session_id) / Path(linked)
                if plan_path.is_file():
                    targets.extend(self._extract_code_paths_from_text(plan_path.read_text(encoding="utf-8", errors="ignore")))

        seen = set()
        ordered = []
        for item in targets:
            normalized = item.replace("\\", "/")
            if normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    def read_context(self, project_root: Path, session_id: str) -> ContextData:
        path = self.context_file(project_root, session_id)
        if not path.exists():
            raise FileNotFoundError(path)
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in CONTEXT_SECTION_ORDER:
            sections.setdefault(section, [])
        return ContextData(session_id=session_id, path=path, sections=sections)

    def update_context(self, project_root: Path, session_id: str, patch: dict[str, list[str]]) -> ContextData:
        data = self.read_context(project_root, session_id)
        for key, value in patch.items():
            if key not in CONTEXT_SECTION_ORDER:
                raise ValueError(f"Unknown context section: {key}")
            data.sections[key] = value
        self.context_file(project_root, session_id).write_text(self._render_context_sections(data.sections), encoding="utf-8")
        return self.read_context(project_root, session_id)

    def list_sessions(self, project_root: Path) -> list[SessionSummary]:
        sessions_dir = self.sessions_root(project_root)
        if not sessions_dir.exists():
            return []

        results: list[SessionSummary] = []
        for session_file in sorted(sessions_dir.glob(f"*/{SESSION_TEMPLATE_NAME}")):
            session_id = session_file.parent.name
            data = self.read_session(project_root, session_id)
            results.append(
                SessionSummary(
                    session_id=session_id,
                    path=session_file.parent,
                    title=self._first_value(data, "Title"),
                    status=self._first_value(data, "Status"),
                    owner=self._first_value(data, "Owner"),
                    goal=self._first_value(data, "Goal"),
                    last_updated=self._first_value(data, "Last Updated"),
                )
            )
        return results

    def read_session(self, project_root: Path, session_id: str) -> SessionData:
        path = self.session_file(project_root, session_id)
        if not path.exists():
            raise FileNotFoundError(path)
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in SESSION_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        return SessionData(session_id=session_id, path=path.parent, sections=sections)

    def select_session(self, project_root: Path, session_id: str) -> SessionSummary:
        session = self.read_session(project_root, session_id)
        return SessionSummary(
            session_id=session.session_id,
            path=session.path,
            title=self._first_value(session, "Title"),
            status=self._first_value(session, "Status"),
            owner=self._first_value(session, "Owner"),
            goal=self._first_value(session, "Goal"),
            last_updated=self._first_value(session, "Last Updated"),
        )

    def list_claims(self, project_root: Path, session_id: str, stale_after_minutes: int = 30) -> list[dict[str, object]]:
        session = self.read_session(project_root, session_id)
        claims = []
        for line in session.sections.get("Active Claims", []):
            claim = self._parse_claim_line(line, stale_after_minutes=stale_after_minutes)
            if claim is not None:
                claims.append(claim)
        return claims

    def claim_session(
        self,
        project_root: Path,
        session_id: str,
        agent_id: str,
        run_id: str,
        mode: str = "active",
    ) -> SessionData:
        session = self.read_session(project_root, session_id)
        claims = self.list_claims(project_root, session_id, stale_after_minutes=10**9)
        updated_claims = []
        replaced = False
        now = self._timestamp()
        for claim in claims:
            if claim["agent_id"] == agent_id and claim["run_id"] == run_id:
                updated_claims.append(
                    {
                        "agent_id": agent_id,
                        "run_id": run_id,
                        "mode": mode,
                        "last_seen": now,
                    }
                )
                replaced = True
            else:
                updated_claims.append(
                    {
                        "agent_id": claim["agent_id"],
                        "run_id": claim["run_id"],
                        "mode": claim["mode"],
                        "last_seen": claim["last_seen"],
                    }
                )
        if not replaced:
            updated_claims.append({"agent_id": agent_id, "run_id": run_id, "mode": mode, "last_seen": now})

        session.sections["Active Claims"] = [self._format_claim_line(item) for item in updated_claims] or ["-"]
        session.sections["Last Updated"] = [f"- {now}"]
        self.session_file(project_root, session_id).write_text(self._render_sections(session.sections), encoding="utf-8")
        return self.read_session(project_root, session_id)

    def release_claim(self, project_root: Path, session_id: str, agent_id: str, run_id: str | None = None) -> SessionData:
        session = self.read_session(project_root, session_id)
        claims = self.list_claims(project_root, session_id, stale_after_minutes=10**9)
        kept = []
        for claim in claims:
            same_agent = claim["agent_id"] == agent_id
            same_run = run_id is None or claim["run_id"] == run_id
            if same_agent and same_run:
                continue
            kept.append(claim)
        session.sections["Active Claims"] = [self._format_claim_line(item) for item in kept] or ["-"]
        session.sections["Last Updated"] = [f"- {self._timestamp()}"]
        self.session_file(project_root, session_id).write_text(self._render_sections(session.sections), encoding="utf-8")
        return self.read_session(project_root, session_id)

    def prune_stale_claims(self, project_root: Path, session_id: str, stale_after_minutes: int = 30) -> SessionData:
        session = self.read_session(project_root, session_id)
        fresh = []
        for claim in self.list_claims(project_root, session_id, stale_after_minutes=stale_after_minutes):
            if not claim["stale"]:
                fresh.append(claim)
        session.sections["Active Claims"] = [self._format_claim_line(item) for item in fresh] or ["-"]
        session.sections["Last Updated"] = [f"- {self._timestamp()}"]
        self.session_file(project_root, session_id).write_text(self._render_sections(session.sections), encoding="utf-8")
        return self.read_session(project_root, session_id)

    def create_session(
        self,
        project_root: Path,
        session_id: str,
        title: str,
        owner: str,
        goal: str,
        scope: str = "-",
        status: str = "active",
        predecessor_session_id: str | None = None,
    ) -> SessionData:
        self._validate_status(status)
        base = self.session_path(project_root, session_id)
        (base / "plans").mkdir(parents=True, exist_ok=True)
        (base / "agents").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)

        session_template = (self.templates_root / SESSION_TEMPLATE_NAME).read_text(encoding="utf-8")
        context_template = (self.templates_root / CONTEXT_TEMPLATE_NAME).read_text(encoding="utf-8")

        session_text = self._render_sections(
            {
                "Title": [f"- {title.strip()}"],
                "Status": [f"- {status}"],
                "Owner": [f"- {owner.strip()}"],
                "Goal": [f"- {goal.strip()}"],
                "Scope": [f"- {scope.strip()}"],
                "Key Memory Links": [f"- `../{predecessor_session_id}/HANDOFF.md`" ] if predecessor_session_id else ["-"],
                "Local Session Links": ["- `context.md`", "- `plans/`", "- `agents/`", "- `artifacts/`"],
                "Active Claims": ["-"],
                "State": ["-"],
                "Upcoming": ["-"],
                "Blockers": ["-"],
                "Last Updated": [f"- {self._timestamp()}"],
            }
        )

        self.session_file(project_root, session_id).write_text(session_text, encoding="utf-8")
        self.context_file(project_root, session_id).write_text(context_template, encoding="utf-8")
        plan_text = self._render_plan_sections(
            {
                "Purpose": [f"- Implement the session goal: {goal.strip()}"],
                "Scope": [f"- {scope.strip()}"],
                "Current State": ["- Session created; detailed execution planning should begin before non-trivial work."],
                "Partial Goals": ["- Break the work into clear intermediate goals before implementation."],
                "End Goal": [f"- {goal.strip()}"],
                "Constraints": ["- Follow project memory, workflow rules, and host/runtime constraints."],
                "Validation": ["- Define concrete verification steps before considering the work complete."],
                "Next Steps": ["- Turn this default plan into a complete detailed plan with partial and end goals."],
            }
        )
        self.plan_file(project_root, session_id).write_text(plan_text, encoding="utf-8")
        handoff_text = self._render_handoff_sections(self._default_handoff_sections(goal.strip(), self._timestamp()))
        self.handoff_file(project_root, session_id).write_text(handoff_text, encoding="utf-8")
        if predecessor_session_id:
            predecessor_link = f"- `{predecessor_session_id}`"
            self.update_handoff(
                project_root,
                session_id,
                {
                    "Related Sessions": [predecessor_link],
                    "Suggested Next Steps": [f"- Review predecessor session `{predecessor_session_id}` and continue from its handoff if that context is still relevant."],
                },
                append=True,
            )
        return self.read_session(project_root, session_id)

    def update_session(self, project_root: Path, session_id: str, patch: dict[str, list[str]]) -> SessionData:
        data = self.read_session(project_root, session_id)
        for key, value in patch.items():
            if key not in SESSION_SECTION_ORDER:
                raise ValueError(f"Unknown session section: {key}")
            if key == "Status" and value:
                first = value[0].strip()
                normalized = first[2:].strip() if first.startswith("-") else first
                self._validate_status(normalized)
            data.sections[key] = value
        data.sections["Last Updated"] = [f"- {self._timestamp()}"]
        self.session_file(project_root, session_id).write_text(self._render_sections(data.sections), encoding="utf-8")
        return self.read_session(project_root, session_id)

    def _first_value(self, data: SessionData, section: str) -> str | None:
        values = data.sections.get(section, [])
        if not values:
            return None
        first = values[0].strip()
        return first[2:].strip() if first.startswith("-") else first

    def _parse_sections(self, text: str) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if line.startswith("## "):
                current = line[3:].strip()
                sections[current] = []
                continue
            if current is not None:
                if line or sections[current]:
                    sections[current].append(line)
        return sections

    def _render_sections(self, sections: dict[str, list[str]]) -> str:
        lines = ["# Session", ""]
        for name in SESSION_SECTION_ORDER:
            lines.append(f"## {name}")
            values = sections.get(name, ["-"])
            if not values:
                values = ["-"]
            lines.extend(values)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_context_sections(self, sections: dict[str, list[str]]) -> str:
        lines = ["# Context", ""]
        for name in CONTEXT_SECTION_ORDER:
            lines.append(f"## {name}")
            values = sections.get(name, [])
            if values:
                lines.extend(values)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_plan_sections(self, sections: dict[str, list[str]]) -> str:
        lines = ["# Plan", ""]
        for name in PLAN_SECTION_ORDER:
            lines.append(f"## {name}")
            values = sections.get(name, ["-"])
            if not values:
                values = ["-"]
            lines.extend(values)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_handoff_sections(self, sections: dict[str, list[str]]) -> str:
        lines = ["# Handoff", ""]
        for name in HANDOFF_SECTION_ORDER:
            lines.append(f"## {name}")
            values = sections.get(name, ["-"])
            if not values:
                values = ["-"]
            lines.extend(values)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _parse_handoff_steps(self, lines: list[str]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        markers = {marker: status for status, marker in self.HANDOFF_STEP_MARKERS.items()}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "-":
                continue
            match = re.match(r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+)\s+@\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\s+(.+)$", stripped)
            if not match:
                legacy_match = re.match(r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+):\s+(.+)$", stripped)
                if legacy_match:
                    marker, step_id, text = legacy_match.groups()
                    result.append({
                        "id": step_id,
                        "status": markers.get(marker, "open"),
                        "text": text.strip(),
                        "updated_at": "",
                    })
                continue
            marker, step_id, updated_at, text = match.groups()
            result.append({
                "id": step_id,
                "status": markers.get(marker, "open"),
                "text": text.strip(),
                "updated_at": updated_at,
            })
        return result

    def _render_handoff_steps(self, steps: list[dict[str, str]]) -> list[str]:
        if not steps:
            return ["-"]
        lines = []
        for step in steps:
            marker = self.HANDOFF_STEP_MARKERS.get(step.get("status", "open"), "[ ]")
            updated_at = step.get("updated_at") or self._timestamp()
            lines.append(f"- {marker} {step['id']} @ {updated_at}: {step['text']}")
        return lines

    def _extract_bullet_path(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped.startswith("-"):
            return None
        value = stripped[1:].strip().strip('`')
        return value or None

    def _extract_code_paths_from_text(self, text: str) -> list[str]:
        matches: list[str] = []
        for candidate in re.findall(r'`([^`\n]+)`', text):
            if self._looks_like_code_path(candidate):
                matches.append(candidate)
        for candidate in re.findall(r'\b[\w./\\-]+\.(?:py|js|ts|jsx|tsx|cs|cshtml|csproj|sql|css|scss|json|yaml|yml|html|razor|go|rs|java|kt|rb|php|sh|ps1)\b', text):
            if self._looks_like_code_path(candidate):
                matches.append(candidate)
        return matches

    def _looks_like_code_path(self, value: str) -> bool:
        normalized = value.replace('\\', '/').strip()
        if normalized.startswith('/.MEMORY/'):
            return False
        return normalized.endswith((
            '.py', '.js', '.ts', '.jsx', '.tsx',
            '.cs', '.cshtml', '.csproj',
            '.sql',
            '.css', '.scss', '.less',
            '.json', '.yaml', '.yml',
            '.html', '.razor',
            '.go', '.rs', '.java', '.kt', '.rb', '.php',
            '.sh', '.ps1', '.bash',
        ))

    def _validate_status(self, status: str) -> None:
        if status not in VALID_SESSION_STATUSES:
            allowed = ", ".join(sorted(VALID_SESSION_STATUSES))
            raise ValueError(f"Invalid session status '{status}'. Allowed: {allowed}")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _parse_claim_line(self, line: str, stale_after_minutes: int) -> dict[str, object] | None:
        stripped = line.strip()
        if not stripped or stripped == "-" or not stripped.startswith("-"):
            return None
        body = stripped[1:].strip().strip('`')
        parts = [part.strip() for part in body.split("|")]
        if len(parts) < 4:
            return None
        agent_id, run_id, mode, last_seen = parts[:4]
        stale = False
        try:
            dt = datetime.strptime(last_seen, "%Y-%m-%d %H:%M")
            stale = (datetime.now() - dt).total_seconds() > stale_after_minutes * 60
        except ValueError:
            stale = False
        return {
            "agent_id": agent_id,
            "run_id": run_id,
            "mode": mode,
            "last_seen": last_seen,
            "stale": stale,
        }

    def _format_claim_line(self, claim: dict[str, object]) -> str:
        return f"- `{claim['agent_id']} | {claim['run_id']} | {claim['mode']} | {claim['last_seen']}`"

    # ── Session journal ──────────────────────────────────────────────

    JOURNAL_FILENAME = "journal.md"

    @staticmethod
    def _journal_config() -> tuple[int, int, set[str], int]:
        """Load journal config from aidocs.toml (cached at import time)."""
        from .config import JOURNAL_MAX_ENTRIES, JOURNAL_EVICT_BATCH, JOURNAL_TRIVIAL_ACTIONS, JOURNAL_MIN_INTENT_LENGTH
        return JOURNAL_MAX_ENTRIES, JOURNAL_EVICT_BATCH, JOURNAL_TRIVIAL_ACTIONS, JOURNAL_MIN_INTENT_LENGTH

    def journal_path(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / self.JOURNAL_FILENAME

    def journal_archive_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "archive" / "sessions" / session_id / "journal-archive.md"

    def read_journal(self, project_root: Path, session_id: str, last_n: int | None = None) -> list[dict[str, str]]:
        """Read journal entries. Returns list of {timestamp, intent, outcome, action_kind}."""
        path = self.journal_path(project_root, session_id)
        if not path.is_file():
            return []
        entries: list[dict[str, str]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = self._parse_journal_line(line)
            if parsed:
                entries.append(parsed)
        if last_n is not None:
            entries = entries[-last_n:]
        return entries

    def write_journal_entry(
        self,
        project_root: Path,
        session_id: str,
        action_kind: str,
        intent: str,
        outcome: str,
        max_entries: int | None = None,
    ) -> dict[str, object]:
        """Append a journal entry. Auto-evicts oldest entries to archive when full.

        Returns {logged: bool, evicted: int, reason: str}.
        """
        max_ent, evict_batch, trivial_actions, min_intent_len = self._journal_config()

        # Significance filter — skip trivial actions
        if action_kind in trivial_actions:
            return {"logged": False, "evicted": 0, "reason": f"trivial action: {action_kind}"}

        # Skip very short intents (greetings, single-word commands)
        clean_intent = intent.strip()
        if len(clean_intent) < min_intent_len:
            return {"logged": False, "evicted": 0, "reason": "intent too short"}

        limit = max_entries or max_ent
        path = self.journal_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing entries
        existing_lines: list[str] = []
        if path.is_file():
            existing_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

        # Evict if at capacity
        evicted = 0
        if len(existing_lines) >= limit:
            evicted = self._evict_journal_entries(project_root, session_id, existing_lines)
            # Re-read after eviction
            existing_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

        # Append new entry
        timestamp = self._timestamp()
        # Truncate intent and outcome to keep entries concise
        short_intent = clean_intent[:120].replace("|", "/").replace("\n", " ")
        short_outcome = outcome.strip()[:120].replace("|", "/").replace("\n", " ")
        entry_line = f"- `{timestamp} | {action_kind} | {short_intent} | {short_outcome}`"

        existing_lines.append(entry_line)
        path.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")

        return {"logged": True, "evicted": evicted, "reason": "ok"}

    def _evict_journal_entries(
        self,
        project_root: Path,
        session_id: str,
        lines: list[str],
    ) -> int:
        """Move oldest entries to archive. Returns number evicted."""
        _, evict_batch, _, _ = self._journal_config()
        batch = min(evict_batch, len(lines))
        if batch <= 0:
            return 0

        to_archive = lines[:batch]
        remaining = lines[batch:]

        # Compress archived entries: group by date, keep just the count + key intents
        archive_path = self.journal_archive_path(project_root, session_id)
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        # Append raw entries to archive (they're already concise 1-liners)
        existing_archive = ""
        if archive_path.is_file():
            existing_archive = archive_path.read_text(encoding="utf-8")
        archive_block = "\n".join(to_archive)
        archive_path.write_text(
            (existing_archive.rstrip() + "\n" + archive_block + "\n").lstrip(),
            encoding="utf-8",
        )

        # Rewrite journal with remaining entries
        journal_path = self.journal_path(project_root, session_id)
        journal_path.write_text("\n".join(remaining) + "\n" if remaining else "", encoding="utf-8")

        return batch

    def _parse_journal_line(self, line: str) -> dict[str, str] | None:
        """Parse a journal line: ``- `timestamp | action_kind | intent | outcome` ``."""
        stripped = line.strip()
        if not stripped.startswith("- `") or not stripped.endswith("`"):
            return None
        inner = stripped[3:-1]
        parts = inner.split(" | ", 3)
        if len(parts) < 4:
            return None
        return {
            "timestamp": parts[0].strip(),
            "action_kind": parts[1].strip(),
            "intent": parts[2].strip(),
            "outcome": parts[3].strip(),
        }
