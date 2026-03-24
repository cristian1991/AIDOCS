from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from .constants import (
    CONTEXT_TEMPLATE_NAME,
    CONTEXT_SECTION_ORDER,
    SESSION_SECTION_ORDER,
    SESSION_TEMPLATE_NAME,
    VALID_SESSION_STATUSES,
)
from .types import ContextData, SessionData, SessionSummary


class SessionStore:
    """File-backed session discovery and mutation."""

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
                "Key Memory Links": ["-"],
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
