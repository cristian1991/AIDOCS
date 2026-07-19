from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .constants import (
    CONTEXT_SECTION_ORDER,
    CONTEXT_TEMPLATE_NAME,
    HANDOFF_FILE_SUFFIX,
    HANDOFF_SECTION_ORDER,
    PLAN_SECTION_ORDER,
    PLAN_TEMPLATE_NAME,
    SESSION_SECTION_ORDER,
    SESSION_TEMPLATE_NAME,
    VALID_SESSION_STATUSES,
)
from .types import (
    ContextData,
    HandoffData,
    PlanData,
    PlanLane,
    PlanPhase,
    PlanStep,
    SessionData,
    SessionSummary,
)

# #363: mask persisted when the output guard itself is unavailable. Unknown is
# not clean — content we could not scan degrades to a mask rather than landing
# unscanned in a git-committed .MEMORY artifact.
GUARD_UNAVAILABLE_MASK = "[REDACTED:guard-unavailable]"


def _scrub_credential_text(text: str) -> str:
    """#363: run the output-guard credential scan over content bound for a
    .MEMORY session artifact and mask any credential-shaped span BEFORE it is
    persisted. .MEMORY journals/handoffs/plans are git-committed — a secret
    that lands there is in repository history forever, so the privacy floor
    must run at write time, not only at mine time.

    Fail-quiet: a guard failure must never break journaling — but on a scan
    error the whole suspicious span (the incoming text) degrades to a mask
    instead of persisting unscanned. Prose without credential-shaped spans
    passes through byte-identical.
    """
    if not text:
        return text
    try:
        from .output_guard import scan_text

        result = scan_text(text, redact=True)
        if result.redacted_text is not None:
            return result.redacted_text
        return text
    except Exception:
        return GUARD_UNAVAILABLE_MASK


def _scrub_credential_lines(lines: list[str]) -> list[str]:
    """Per-line scrub for section-value lists — keeps document structure
    (headers/bullets come from code) while masking credential-shaped spans."""
    return [_scrub_credential_text(line) for line in lines]


class SessionStore:
    """File-backed session discovery and mutation."""

    HANDOFF_STEP_MARKERS = {
        "open": "[ ]",
        "completed": "[x]",
        "done": "[x]",
        "failed": "[!]",
        "reset": "[~]",
        "stale": "[?]",
    }
    HANDOFF_MARKER_STATES = {
        "[ ]": "open",
        "[~]": "reset",
        "[!]": "failed",
        "[?]": "stale",
        "[x]": "completed",
        "[X]": "completed",
    }
    ROADMAP_STEP_MARKERS = {
        "open": "[ ]",
        "in_progress": "[~]",
        "pending_user_feedback": "[>]",
        "completed": "[x]",
        "blocked": "[!]",
    }
    ROADMAP_MARKER_STATES = {
        "[ ]": "open",
        "[~]": "in_progress",
        "[>]": "pending_user_feedback",
        "[!]": "blocked",
        "[x]": "completed",
        "[X]": "completed",
    }
    PLAN_MARKER_STATES = {
        "[ ]": "open",
        "[~]": "in_progress",
        "[>]": "awaiting_feedback",
        "[!]": "blocked",
        "[x]": "completed",
        "[X]": "completed",
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
            "Current State": [
                "- Session created; no implementation handoff has been recorded yet.",
            ],
            "What Was Done": ["- Session scaffold created."],
            "What Failed / Dead Ends": ["-"],
            "What Matters Now": ["- Convert this into a meaningful handoff once real work begins."],
            "Open Questions": ["-"],
            "Risks and Blockers": ["-"],
            "Relevant Files": ["-"],
            "Estimated Effort": ["-"],
            "Suggested Next Steps": [
                "- Start with a complete detailed plan and update the handoff as work progresses.",
            ],
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
            canonical.write_text(
                source.read_text(encoding="utf-8", errors="ignore"),
                encoding="utf-8",
            )
            return
        session = self.read_session(project_root, session_id)
        goal = self._first_value(session, "Goal") or "Unknown session goal"
        created_at = self._timestamp()
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(
            self._render_handoff_sections(self._default_handoff_sections(goal, created_at)),
            encoding="utf-8",
        )

    def read_handoff(self, project_root: Path, session_id: str) -> HandoffData:
        canonical = self.handoff_file(project_root, session_id)
        if not canonical.exists():
            self._ensure_handoff(project_root, session_id)
        path = canonical
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in HANDOFF_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        return HandoffData(session_id=session_id, path=path, sections=sections)

    def read_handoff_optional(self, project_root: Path, session_id: str) -> HandoffData | None:
        path = self._handoff_source_file(project_root, session_id)
        if path is None or not path.exists():
            return None
        sections = self._parse_sections(path.read_text(encoding="utf-8", errors="ignore"))
        for section in HANDOFF_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        return HandoffData(session_id=session_id, path=path, sections=sections)

    def update_handoff(
        self,
        project_root: Path,
        session_id: str,
        patch: dict[str, list[str]],
        append: bool = False,
    ) -> HandoffData:
        data = self.read_handoff(project_root, session_id)
        for key, value in patch.items():
            if key not in HANDOFF_SECTION_ORDER:
                raise ValueError(f"Unknown handoff section: {key}")
            if append:
                existing = [
                    item
                    for item in data.sections.get(key, ["-"])
                    if item.strip() and item.strip() != "-"
                ]
                incoming = [
                    item for item in (value or ["-"]) if item.strip() and item.strip() != "-"
                ]
                data.sections[key] = existing + incoming or ["-"]
            else:
                data.sections[key] = value or ["-"]
        if "Freshness" not in patch:
            data.sections["Freshness"] = [f"- Updated {self._timestamp()} automatically."]
        return self._write_handoff_sections(project_root, session_id, data.sections)

    def read_handoff_steps(self, project_root: Path, session_id: str) -> list[dict[str, str]]:
        handoff = self.read_handoff(project_root, session_id)
        return self._parse_handoff_steps(handoff.sections.get("Steps", []))

    def read_handoff_steps_optional(
        self,
        project_root: Path,
        session_id: str,
    ) -> list[dict[str, str]]:
        handoff = self.read_handoff_optional(project_root, session_id)
        if handoff is None:
            return []
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
        normalized_status = self._normalize_handoff_step_status(status)
        if normalized_status not in self.HANDOFF_STEP_MARKERS:
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
                        "status": normalized_status,
                        "text": text if text is not None else item["text"],
                        "updated_at": now,
                    },
                )
            else:
                updated.append(item)
        if not found:
            if not append and existing:
                updated = []
            updated.append(
                {
                    "id": step_id,
                    "status": normalized_status,
                    "text": text or step_id,
                    "updated_at": now,
                },
            )
        step_lines = self._render_handoff_steps(updated)
        return self.update_handoff(project_root, session_id, {"Steps": step_lines}, append=False)

    def normalize_handoff_steps(self, project_root: Path, session_id: str) -> dict[str, object]:
        handoff = self.read_handoff(project_root, session_id)
        original_lines = list(handoff.sections.get("Steps", ["-"]))
        normalized_lines: list[str] = []
        changed: list[dict[str, str]] = []
        untouched: list[str] = []

        for line in original_lines:
            normalized_line = self._normalize_handoff_step_line(line)
            if normalized_line is None:
                normalized_lines.append(line)
                stripped = line.strip()
                if stripped and stripped != "-":
                    untouched.append(line)
                continue
            normalized_lines.append(normalized_line)
            if normalized_line != line:
                changed.append({"from": line, "to": normalized_line})
            else:
                untouched.append(line)

        if not normalized_lines:
            normalized_lines = ["-"]

        if changed:
            handoff.sections["Steps"] = normalized_lines
            handoff = self._write_handoff_sections(project_root, session_id, handoff.sections)

        return {
            "session_id": session_id,
            "path": str(handoff.path),
            "status": "normalized" if changed else "unchanged",
            "changed": changed,
            "untouched": untouched,
            "normalized_lines": normalized_lines,
        }

    def read_plan(self, project_root: Path, session_id: str) -> PlanData:
        path = self.plan_file(project_root, session_id)
        if not path.exists():
            # Graceful fallback — return empty plan instead of throwing
            return PlanData(
                session_id=session_id,
                path=path,
                sections={s: ["-"] for s in PLAN_SECTION_ORDER},
                phases=[],
                lanes={},
            )
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in PLAN_SECTION_ORDER:
            sections.setdefault(section, ["-"])
        # Accept both the canonical "Steps" header AND the alias
        # "Lane graph" — the lane-aware plan template surfaces the
        # section as "Lane graph" for readability; operators writing
        # plans from that template would otherwise get has_lanes=False
        # because the parser only looked at "Steps". Either section
        # feeds the same lane-aware parser. (Bug fix 2026-04-20.)
        steps_source = sections.get("Steps") or sections.get("Lane graph") or []
        phases, lanes = self._parse_lane_aware_steps(steps_source)
        return PlanData(
            session_id=session_id,
            path=path,
            sections=sections,
            phases=phases,
            lanes=lanes,
        )

    def read_plan_optional(self, project_root: Path, session_id: str) -> PlanData | None:
        path = self.plan_file(project_root, session_id)
        if not path.exists():
            return None
        return self.read_plan(project_root, session_id)

    def list_session_plans(self, project_root: Path, session_id: str) -> list[dict[str, object]]:
        """Per-plan progress aggregator for the session's plans/ dir.

        Walks every `*.md` under <session>/plans/, counts checkbox lines
        ([ ] open, [x]/[X]/[d]/[D] done), derives a status label and a
        percent. Used by session_connect's conductor branch to surface
        the shelf — names + status only, no bodies. Bodies are pulled
        via ai_plan(mode='read', name=...) on demand.
        """
        plans_dir = self.session_path(project_root, session_id) / "plans"
        if not plans_dir.is_dir():
            return []
        plans: list[dict[str, object]] = []
        for path in sorted(plans_dir.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            total = 0
            done = 0
            for line in text.splitlines():
                m = re.match(r"^\s*-\s+\[([^\]]*)\]\s+", line)
                if m:
                    total += 1
                    marker = m.group(1).strip()
                    if marker in ("x", "X", "d", "D"):
                        done += 1
            if total == 0:
                status = "no_steps"
                percent = 0
            elif done == 0:
                status = "open"
                percent = 0
            elif done == total:
                status = "done"
                percent = 100
            else:
                status = "in_progress"
                percent = (done * 100) // total
            plans.append(
                {
                    "name": path.stem,
                    "status": status,
                    "progress": f"{done}/{total}",
                    "percent": percent,
                },
            )
        return plans

    def scan_lane_plans(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, Any]:
        """Scan plans/*.md for lane-aware plan files (conductor-facing).

        Fixes backlog #1: conductor-side tools previously read only
        PLAN.md; lanes in any other plans/*.md were invisible to
        plan_conductor_status / plan_dispatch_next / plan_conductor_graph.

        Returns:
            {"plans": [PlanData, ...]} on success (zero or one entry)
            {"plans": [], "error": "<code>", "files": [...]} on ambiguity

        Rules (no-unresolved-ambiguity invariant):
            - Zero files with lanes → {"plans": []}
            - Exactly one file with lanes → {"plans": [that_plan]}
            - PLAN.md has lanes AND another file has lanes → error
            - Two or more non-PLAN.md files have lanes → error

        Non-conductor callers continue to use read_plan() (single PLAN.md).
        Only conductor dispatch/status/graph should use scan_lane_plans.

        """
        plans_dir = self.session_path(project_root, session_id) / "plans"
        if not plans_dir.is_dir():
            return {"plans": []}
        lane_aware: list[PlanData] = []
        for plan_path in sorted(plans_dir.glob("*.md")):
            try:
                text = plan_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            try:
                sections = self._parse_sections(text)
            except Exception:
                continue
            for section in PLAN_SECTION_ORDER:
                sections.setdefault(section, ["-"])
            steps_source = sections.get("Steps") or sections.get("Lane graph") or []
            try:
                phases, lanes = self._parse_lane_aware_steps(steps_source)
            except Exception:
                continue
            if not lanes:
                continue
            lane_aware.append(
                PlanData(
                    session_id=session_id,
                    path=plan_path,
                    sections=sections,
                    phases=phases,
                    lanes=lanes,
                ),
            )
        if len(lane_aware) <= 1:
            return {"plans": lane_aware}
        return {
            "plans": [],
            "error": "multiple_lane_aware_plans",
            "files": [str(p.path.relative_to(project_root)).replace("\\", "/") for p in lane_aware],
        }

    def roadmap_candidates(self, project_root: Path, *, mutable_only: bool = False) -> list[Path]:
        """Find all planning documents across project and memory.

        Args:
            mutable_only: If True, exclude plain ROADMAP.md — only return
                versioned roadmaps (ROADMAP_*.md) and .MEMORY/ planning files
                that AIDOCS owns and can safely mutate.

        """
        candidates: list[Path] = []
        # Root roadmaps
        for f in sorted(project_root.glob("ROADMAP*.md")):
            if f.is_file():
                if mutable_only and f.name == "ROADMAP.md":
                    continue
                candidates.append(f)
        # Project-level and memory-level planning dirs
        for subdir in [
            "docs/plans",
            "docs/specs",
            ".MEMORY/roadmaps",
            ".MEMORY/specs",
            ".MEMORY/plans",
        ]:
            d = project_root / subdir
            if d.is_dir():
                for f in sorted(d.glob("*.md")):
                    if f.is_file():
                        candidates.append(f)
        return candidates

    def list_planning_docs(self, project_root: Path) -> list[dict[str, object]]:
        """List all planning documents with checkbox status summary."""
        docs: list[dict[str, object]] = []
        for path in self.roadmap_candidates(project_root):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            total = 0
            done = 0
            open_count = 0
            for line in text.splitlines():
                match = re.match(r"^\s*-\s+\[([^\]]*)\]\s+", line)
                if match:
                    total += 1
                    marker = match.group(1).strip()
                    if marker in ("x", "X"):
                        done += 1
                    elif marker == " ":
                        open_count += 1
            try:
                rel = str(path.relative_to(project_root)).replace("\\", "/")
            except ValueError:
                rel = str(path)
            docs.append(
                {
                    "path": rel,
                    "total_steps": total,
                    "done": done,
                    "open": open_count,
                    "other": total - done - open_count,
                },
            )
        return docs

    def mark_planning_step(
        self,
        project_root: Path,
        path: str,
        line_number: int,
        status: str = "done",
    ) -> dict[str, object]:
        """Toggle a checkbox in a planning document. Status: 'done', 'open', 'skip', 'in_progress'."""
        marker_map = {
            "done": "[x]",
            "open": "[ ]",
            "skip": "[~]",
            "in_progress": "[>]",
            "blocked": "[!]",
        }
        new_marker = marker_map.get(status)
        if not new_marker:
            return {
                "success": False,
                "error": f"Unknown status: {status}. Use: {', '.join(marker_map.keys())}",
            }

        abs_path = (project_root / path.replace("\\", "/")).resolve()
        # #133: NARROWED path-policy — confine the toggle to the project root and
        # reject traversal. Deliberately NOT the full file_ops._resolve_path
        # write-fence: mark_planning_step legitimately targets planning docs
        # OUTSIDE .MEMORY (ROADMAP.md, repo-level plans), which the self-edit
        # fence would refuse in the live AIDOCS source install — that would be a
        # user-access regression. Co-conductor ruling: traversal + confinement is
        # the right gate for this caller-supplied path; the full write-fence is
        # reserved for the .MEMORY/session writers.
        try:
            abs_path.relative_to(project_root.resolve())
        except ValueError:
            return {"success": False, "error": f"Path escapes the project root: {path}"}
        if not abs_path.is_file():
            return {"success": False, "error": f"File not found: {path}"}

        lines = abs_path.read_text(encoding="utf-8").splitlines()
        if line_number < 1 or line_number > len(lines):
            return {"success": False, "error": f"Line {line_number} out of range"}

        line = lines[line_number - 1]
        match = re.match(r"^(\s*-\s+)\[([^\]]*)\](\s+.*)$", line)
        if not match:
            return {"success": False, "error": f"Line {line_number} is not a checkbox step"}

        prefix, old_marker, rest = match.groups()
        lines[line_number - 1] = f"{prefix}{new_marker}{rest}"
        abs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # #133: audit the toggle — this direct write was previously invisible.
        try:
            from .execution_index_store import ExecutionIndexStore

            ExecutionIndexStore().record_event(
                project_root,
                event_kind="session.plan.step.toggled",
                source_kind="session_store",
                capability_name="planning_step_mark",
                action_kind="toggle",
                target_entity=path,
                status=status,
                payload={
                    "path": path,
                    "line": line_number,
                    "old_status": old_marker.strip(),
                    "new_status": status,
                },
            )
        except Exception:
            pass

        return {
            "success": True,
            "path": path,
            "line": line_number,
            "old_status": old_marker.strip(),
            "new_status": status,
        }

    def read_roadmap_steps(self, project_root: Path) -> list[dict[str, object]]:
        for path in self.roadmap_candidates(project_root):
            if not path.exists():
                continue
            steps: list[dict[str, object]] = []
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(),
                start=1,
            ):
                match = re.match(r"^\s*-\s+(\[[ xX~>!]\])\s+(.+?)\s*$", line)
                if not match:
                    continue
                marker, text = match.groups()
                status = self.ROADMAP_MARKER_STATES.get(marker)
                if status is None or status == "completed":
                    continue
                try:
                    relative_path = str(path.relative_to(project_root)).replace("\\", "/")
                except ValueError:
                    relative_path = str(path).replace("\\", "/")
                steps.append(
                    {
                        "text": text.strip(),
                        "status": status,
                        "line_number": line_number,
                        "path": relative_path,
                    },
                )
            if steps:
                return steps
        return []

    def find_roadmap_step_matches(
        self,
        project_root: Path,
        step_text: str,
    ) -> list[dict[str, object]]:
        normalized_target = self._normalize_step_text(step_text)
        matches: list[dict[str, object]] = []
        for path in self.roadmap_candidates(project_root, mutable_only=True):
            if not path.exists():
                continue
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(),
                start=1,
            ):
                match = re.match(r"^\s*-\s+(\[[ xX~>!]\])\s+(.+?)\s*$", line)
                if not match:
                    continue
                marker, text = match.groups()
                status = self.ROADMAP_MARKER_STATES.get(marker)
                if status is None:
                    continue
                if self._normalize_step_text(text) != normalized_target:
                    continue
                try:
                    relative_path = str(path.relative_to(project_root)).replace("\\", "/")
                except ValueError:
                    relative_path = str(path).replace("\\", "/")
                matches.append(
                    {
                        "text": text.strip(),
                        "status": status,
                        "line_number": line_number,
                        "path": relative_path,
                    },
                )
        return matches

    def update_roadmap_step_state(
        self,
        project_root: Path,
        step_text: str,
        status: str,
    ) -> dict[str, object]:
        marker = self.ROADMAP_STEP_MARKERS.get(status)
        if marker is None:
            raise ValueError(f"Unknown roadmap step status: {status}")
        matches = self.find_roadmap_step_matches(project_root, step_text)
        if len(matches) > 1:
            raise ValueError(f"Ambiguous actionable roadmap step matched: {step_text}")
        if not matches:
            raise ValueError(f"No actionable roadmap step matched: {step_text}")
        normalized_target = self._normalize_step_text(step_text)
        for path in self.roadmap_candidates(project_root, mutable_only=True):
            if not path.exists():
                continue
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for index, line in enumerate(lines):
                match = re.match(r"^(\s*-\s+)(\[[ xX~>!]\])(\s+)(.+?)\s*$", line)
                if not match:
                    continue
                prefix, _, spacing, text = match.groups()
                if self._normalize_step_text(text) != normalized_target:
                    continue
                lines[index] = f"{prefix}{marker}{spacing}{text.strip()}"
                rendered = "\n".join(lines).rstrip() + "\n"
                path.write_text(rendered, encoding="utf-8")
                try:
                    relative_path = str(path.relative_to(project_root)).replace("\\", "/")
                except ValueError:
                    relative_path = str(path).replace("\\", "/")
                return {
                    "text": text.strip(),
                    "status": status,
                    "path": relative_path,
                    "line_number": index + 1,
                }
        raise ValueError(f"No actionable roadmap step matched: {step_text}")

    def _guard_and_audit_memory_write(
        self,
        project_root: Path,
        session_id: str,
        abs_path: Path,
        *,
        event_kind: str,
        sections: object,
    ) -> Path:
        """#133: route a .MEMORY/session artifact write through the path-policy
        gate (traversal / root-confinement / protected-file / DNT sentinel) AND
        emit a lightweight audit event, so these direct writes are no longer
        invisible to the audit chain. Returns the validated absolute path; raises
        (ValueError) if the path policy refuses. .MEMORY/sessions/** clears every
        gate except the DNT/sentinel guard — normal session writes pass; a
        traversal or a sentinel-protected artifact is refused. Audit emission is
        best-effort: a failure never blocks the write."""
        from .file_ops import _resolve_path

        relpath = abs_path.relative_to(project_root).as_posix()
        resolved = _resolve_path(project_root, relpath, write=True)
        try:
            from .execution_index_store import ExecutionIndexStore

            _sections = sorted(str(k) for k in sections) if sections else []
            ExecutionIndexStore().record_event(
                project_root,
                event_kind=event_kind,
                source_kind="session_store",
                session_id=session_id or None,
                action_kind="update",
                target_entity=relpath,
                status="updated",
                payload={"session_id": session_id, "sections": _sections},
            )
        except Exception:
            pass
        return resolved

    def update_plan(
        self,
        project_root: Path,
        session_id: str,
        patch: dict[str, list[str]],
    ) -> PlanData:
        path = self.plan_file(project_root, session_id)
        if path.exists():
            data = self.read_plan(project_root, session_id)
        else:
            # Bootstrap empty plan so plan_create_from_spec can populate it
            path.parent.mkdir(parents=True, exist_ok=True)
            data = PlanData(
                session_id=session_id,
                path=path,
                sections={s: ["-"] for s in PLAN_SECTION_ORDER},
                phases=[],
                lanes={},
            )
        for key, value in patch.items():
            if key not in PLAN_SECTION_ORDER:
                raise ValueError(f"Unknown plan section: {key}")
            data.sections[key] = value or ["-"]
        resolved = self._guard_and_audit_memory_write(
            project_root,
            session_id,
            self.plan_file(project_root, session_id),
            event_kind="session.plan.section.replaced",
            sections=patch,
        )
        # #363: credential scrub per value line before persisting.
        data.sections = {k: _scrub_credential_lines(v) for k, v in data.sections.items()}
        resolved.write_text(
            self._render_plan_sections(data.sections),
            encoding="utf-8",
        )
        return self.read_plan(project_root, session_id)

    def preview_plan_feedback_sections(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        plan = self.read_plan(project_root, session_id)
        existing_lines = list(plan.sections.get("Steps", []))
        normalized_lines: list[str] = []
        changed: list[dict[str, str]] = []
        untouched: list[str] = []
        original_prose: list[str] = []
        seen_existing = {line.strip() for line in existing_lines}

        for line in existing_lines:
            parsed = self._parse_plan_checkbox_line(line)
            if parsed is not None:
                stripped = line.strip()
                if stripped:
                    untouched.append(stripped)
                continue
            stripped = line.strip()
            if self._is_lane_metadata_line(stripped):
                untouched.append(stripped)
                continue
            if not stripped or stripped == "-" or not stripped.startswith("-"):
                if stripped and stripped != "-":
                    untouched.append(line)
                continue
            prose = stripped[1:].strip()
            if not prose:
                continue
            original_prose.append(prose)
            normalized = f"- [>] {self._normalize_plan_prose_text(prose)}"
            if normalized.strip() in seen_existing:
                untouched.append(stripped)
                continue
            normalized_lines.append(normalized)
            changed.append({"from": stripped, "to": normalized})
            seen_existing.add(normalized.strip())

        return {
            "session_id": session_id,
            "path": str(plan.path),
            "status": "awaiting_feedback" if normalized_lines else "unchanged",
            "changed": changed,
            "untouched": untouched,
            "original_prose": original_prose,
            "normalized_lines": normalized_lines,
        }

    def normalize_plan_feedback_sections(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        result = self.preview_plan_feedback_sections(project_root, session_id)
        normalized_lines = list(result.get("normalized_lines", []))
        if normalized_lines:
            plan = self.read_plan(project_root, session_id)
            existing_lines = list(plan.sections.get("Steps", []))
            plan = self.update_plan(
                project_root,
                session_id,
                {"Steps": existing_lines + normalized_lines},
            )
            result["path"] = str(plan.path)
        return result

    def normalize_session_artifacts(self, project_root: Path, session_id: str) -> dict[str, object]:
        handoff_steps = self.normalize_handoff_steps(project_root, session_id)
        plan_feedback_sections = self.normalize_plan_feedback_sections(project_root, session_id)
        changed: list[str] = []
        untouched: list[str] = []
        for artifact_name, result in (
            ("handoff_steps", handoff_steps),
            ("plan_feedback_sections", plan_feedback_sections),
        ):
            if result.get("status") in {"normalized", "awaiting_feedback"}:
                changed.append(artifact_name)
            else:
                untouched.append(artifact_name)
        return {
            "session_id": session_id,
            "changed": changed,
            "untouched": untouched,
            "handoff_steps": handoff_steps,
            "plan_feedback_sections": plan_feedback_sections,
        }

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
                targets.extend(
                    self._extract_code_paths_from_text(
                        plan_file.read_text(encoding="utf-8", errors="ignore"),
                    ),
                )

        # key memory links inside SESSION.md may point to plan files within the session
        for line in session.sections.get("Key Memory Links", []):
            linked = self._extract_bullet_path(line)
            if not linked:
                continue
            if linked.startswith("plans/"):
                plan_path = self.session_path(project_root, session_id) / Path(linked)
                if plan_path.is_file():
                    targets.extend(
                        self._extract_code_paths_from_text(
                            plan_path.read_text(encoding="utf-8", errors="ignore"),
                        ),
                    )

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
            return ContextData(
                session_id=session_id,
                path=path,
                sections={s: [] for s in CONTEXT_SECTION_ORDER},
            )
        sections = self._parse_sections(path.read_text(encoding="utf-8"))
        for section in CONTEXT_SECTION_ORDER:
            sections.setdefault(section, [])
        return ContextData(session_id=session_id, path=path, sections=sections)

    def update_context(
        self,
        project_root: Path,
        session_id: str,
        patch: dict[str, list[str]],
    ) -> ContextData:
        data = self.read_context(project_root, session_id)
        for key, value in patch.items():
            if key not in CONTEXT_SECTION_ORDER:
                raise ValueError(f"Unknown context section: {key}")
            data.sections[key] = value
        resolved = self._guard_and_audit_memory_write(
            project_root,
            session_id,
            self.context_file(project_root, session_id),
            event_kind="session.context.updated",
            sections=patch,
        )
        # #363: credential scrub per value line before persisting.
        data.sections = {k: _scrub_credential_lines(v) for k, v in data.sections.items()}
        resolved.write_text(
            self._render_context_sections(data.sections),
            encoding="utf-8",
        )
        return self.read_context(project_root, session_id)

    def list_sessions(self, project_root: Path) -> list[SessionSummary]:
        # Authority for WHICH sessions exist is the SQLite session_membership
        # table, NOT the presence of SESSION.md files. SESSION.md is optional
        # metadata/export: read for display when present; a member whose
        # SESSION.md was deleted still EXISTS (surfaced with degraded
        # metadata), and a stray unregistered SESSION.md is NOT listed.
        from .session_membership_store import SessionMembershipStore

        member_ids = SessionMembershipStore().list_members(project_root)
        results: list[SessionSummary] = []
        for session_id in member_ids:
            path = self.session_path(project_root, session_id)
            try:
                data = self.read_session(project_root, session_id)
                results.append(
                    SessionSummary(
                        session_id=session_id,
                        path=path,
                        title=self._first_value(data, "Title"),
                        status=self._first_value(data, "Status"),
                        owner=self._first_value(data, "Owner"),
                        goal=self._first_value(data, "Goal"),
                        last_updated=self._first_value(data, "Last Updated"),
                    ),
                )
            except FileNotFoundError:
                # Member in SQL but the exported record is gone — the session
                # still exists; surface it rather than dropping it (authority
                # is SQL, and the file is not authority).
                results.append(
                    SessionSummary(
                        session_id=session_id,
                        path=path,
                        title=None,
                        status=None,
                        owner=None,
                        goal=None,
                        last_updated=None,
                    ),
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
        # Existence authority is SQLite membership, not the SESSION.md file.
        # A non-member raises (no such session); a member with a missing
        # SESSION.md export resolves to a degraded summary rather than failing.
        from .session_membership_store import SessionMembershipStore

        if not SessionMembershipStore().is_member(project_root, session_id):
            raise FileNotFoundError(self.session_file(project_root, session_id))
        try:
            session = self.read_session(project_root, session_id)
        except FileNotFoundError:
            return SessionSummary(
                session_id=session_id,
                path=self.session_path(project_root, session_id),
                title=None,
                status=None,
                owner=None,
                goal=None,
                last_updated=None,
            )
        return SessionSummary(
            session_id=session.session_id,
            path=session.path,
            title=self._first_value(session, "Title"),
            status=self._first_value(session, "Status"),
            owner=self._first_value(session, "Owner"),
            goal=self._first_value(session, "Goal"),
            last_updated=self._first_value(session, "Last Updated"),
        )

    def list_claims(
        self,
        project_root: Path,
        session_id: str,
        stale_after_minutes: int = 30,
    ) -> list[dict[str, object]]:
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
                    },
                )
                replaced = True
            else:
                updated_claims.append(
                    {
                        "agent_id": claim["agent_id"],
                        "run_id": claim["run_id"],
                        "mode": claim["mode"],
                        "last_seen": claim["last_seen"],
                    },
                )
        if not replaced:
            updated_claims.append(
                {"agent_id": agent_id, "run_id": run_id, "mode": mode, "last_seen": now},
            )

        session.sections["Active Claims"] = [
            self._format_claim_line(item) for item in updated_claims
        ] or ["-"]
        session.sections["Last Updated"] = [f"- {now}"]
        self.session_file(project_root, session_id).write_text(
            self._render_sections(session.sections),
            encoding="utf-8",
        )
        return self.read_session(project_root, session_id)

    def release_claim(
        self,
        project_root: Path,
        session_id: str,
        agent_id: str,
        run_id: str | None = None,
    ) -> SessionData:
        session = self.read_session(project_root, session_id)
        claims = self.list_claims(project_root, session_id, stale_after_minutes=10**9)
        kept = []
        for claim in claims:
            same_agent = claim["agent_id"] == agent_id
            same_run = run_id is None or claim["run_id"] == run_id
            if same_agent and same_run:
                continue
            kept.append(claim)
        session.sections["Active Claims"] = [self._format_claim_line(item) for item in kept] or [
            "-",
        ]
        session.sections["Last Updated"] = [f"- {self._timestamp()}"]
        self.session_file(project_root, session_id).write_text(
            self._render_sections(session.sections),
            encoding="utf-8",
        )
        return self.read_session(project_root, session_id)

    def prune_stale_claims(
        self,
        project_root: Path,
        session_id: str,
        stale_after_minutes: int = 30,
    ) -> SessionData:
        session = self.read_session(project_root, session_id)
        fresh = []
        for claim in self.list_claims(
            project_root,
            session_id,
            stale_after_minutes=stale_after_minutes,
        ):
            if not claim["stale"]:
                fresh.append(claim)
        session.sections["Active Claims"] = [self._format_claim_line(item) for item in fresh] or [
            "-",
        ]
        session.sections["Last Updated"] = [f"- {self._timestamp()}"]
        self.session_file(project_root, session_id).write_text(
            self._render_sections(session.sections),
            encoding="utf-8",
        )
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
        # #363: operator/agent-supplied prose is persisted verbatim into
        # git-committed .MEMORY artifacts — scrub credential-shaped spans first.
        title = _scrub_credential_text(title)
        owner = _scrub_credential_text(owner)
        goal = _scrub_credential_text(goal)
        scope = _scrub_credential_text(scope)
        base = self.session_path(project_root, session_id)
        (base / "plans").mkdir(parents=True, exist_ok=True)
        (base / "agents").mkdir(parents=True, exist_ok=True)
        (base / "artifacts").mkdir(parents=True, exist_ok=True)

        context_template = (self.templates_root / CONTEXT_TEMPLATE_NAME).read_text(encoding="utf-8")

        session_text = self._render_sections(
            {
                "Title": [f"- {title.strip()}"],
                "Status": [f"- {status}"],
                "Owner": [f"- {owner.strip()}"],
                "Goal": [f"- {goal.strip()}"],
                "Scope": [f"- {scope.strip()}"],
                "Key Memory Links": [f"- `../{predecessor_session_id}/HANDOFF.md`"]
                if predecessor_session_id
                else ["-"],
                "Local Session Links": [
                    "- `context.md`",
                    "- `plans/`",
                    "- `agents/`",
                    "- `artifacts/`",
                ],
                "Active Claims": ["-"],
                "State": ["-"],
                "Upcoming": ["-"],
                "Blockers": ["-"],
                "Last Updated": [f"- {self._timestamp()}"],
            },
        )

        self.session_file(project_root, session_id).write_text(session_text, encoding="utf-8")
        self.context_file(project_root, session_id).write_text(context_template, encoding="utf-8")
        plan_text = self._render_plan_sections(
            {
                "Purpose": [f"- Implement the session goal: {goal.strip()}"],
                "Scope": [f"- {scope.strip()}"],
                "Current State": [
                    "- Session created; detailed execution planning should begin before non-trivial work.",
                ],
                "Partial Goals": [
                    "- Break the work into clear intermediate goals before implementation.",
                ],
                "End Goal": [f"- {goal.strip()}"],
                "Constraints": [
                    "- Follow project memory, workflow rules, and host/runtime constraints.",
                ],
                "Validation": [
                    "- Define concrete verification steps before considering the work complete.",
                ],
                "Next Steps": [
                    "- Turn this default plan into a complete detailed plan with partial and end goals.",
                ],
            },
        )
        self.plan_file(project_root, session_id).write_text(plan_text, encoding="utf-8")
        handoff_text = self._render_handoff_sections(
            self._default_handoff_sections(goal.strip(), self._timestamp()),
        )
        self.handoff_file(project_root, session_id).write_text(handoff_text, encoding="utf-8")
        # Session-scoped settings live in SQLite with scope=session (set via dashboard)
        # Membership/existence authority is SQLite, not SESSION.md presence:
        # register so session_belongs (the bind gate) recognizes this session.
        # SESSION.md above is the exported verbatim record, not the authority.
        from .session_membership_store import SessionMembershipStore

        SessionMembershipStore().register(project_root, session_id, source="create")

        if predecessor_session_id:
            predecessor_link = f"- `{predecessor_session_id}`"
            self.update_handoff(
                project_root,
                session_id,
                {
                    "Related Sessions": [predecessor_link],
                    "Suggested Next Steps": [
                        f"- Review predecessor session `{predecessor_session_id}` and continue from its handoff if that context is still relevant.",
                    ],
                },
                append=True,
            )
        return self.read_session(project_root, session_id)

    def update_session(
        self,
        project_root: Path,
        session_id: str,
        patch: dict[str, list[str]],
    ) -> SessionData:
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
        # #363: credential scrub per value line before persisting.
        data.sections = {k: _scrub_credential_lines(v) for k, v in data.sections.items()}
        self.session_file(project_root, session_id).write_text(
            self._render_sections(data.sections),
            encoding="utf-8",
        )
        return self.read_session(project_root, session_id)

    def _first_value(self, data: SessionData, section: str) -> str | None:
        values = data.sections.get(section, [])
        if not values:
            return None
        first = values[0].strip()
        return first[2:].strip() if first.startswith("-") else first

    @staticmethod
    def _parse_sections(text: str) -> dict[str, list[str]]:
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

    @staticmethod
    def _compact_section_values(values: list[str]) -> list[str]:
        """#475 padding-writer fix: normalize a section body for writing.

        Drops leading/trailing blank lines and collapses interior blank
        runs to a single blank line. Without this, every read-modify-write
        cycle re-appended the parsed trailing separator blank PLUS a fresh
        one (+1 blank line per section per write) — War AZ read a
        10k-blank-line SESSION.md. Write-side only: _parse_sections stays
        tolerant of old padded files.
        """
        out: list[str] = []
        pending_blank = False
        for raw in values:
            if not raw.strip():
                pending_blank = bool(out)
                continue
            if pending_blank:
                out.append("")
                pending_blank = False
            out.append(raw)
        return out

    def _render_sections(self, sections: dict[str, list[str]]) -> str:
        lines = ["# Session", ""]
        for name in SESSION_SECTION_ORDER:
            lines.append(f"## {name}")
            values = self._compact_section_values(sections.get(name, ["-"]))
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
        """Render plan sections preserving operator-authored custom
        sections (e.g. '## Why this beat exists', '## Goal',
        '## Out of scope', '## Lane graph'). Previously this rendered
        ONLY PLAN_SECTION_ORDER entries, so every update_plan() call
        silently wiped custom sections — task_complete would then
        reset a lane-aware plan back to its skeleton. (Bug fix
        2026-04-20.)

        Emission order: canonical sections in PLAN_SECTION_ORDER, then
        any unknown sections in their original parse-order. This keeps
        legacy plans stable while making lane-aware / free-form plans
        survive task-lifecycle updates.
        """
        lines = ["# Plan", ""]
        emitted: set[str] = set()
        for name in PLAN_SECTION_ORDER:
            lines.append(f"## {name}")
            values = sections.get(name, ["-"])
            if not values:
                values = ["-"]
            lines.extend(values)
            lines.append("")
            emitted.add(name)
        # Preserve custom sections the operator / plan template added.
        # Iteration order of `sections` is insertion order (dict in
        # Python 3.7+), which mirrors the markdown file's order.
        for name, values in sections.items():
            if name in emitted:
                continue
            lines.append(f"## {name}")
            lines.extend(values or ["-"])
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

    def _write_handoff_sections(
        self,
        project_root: Path,
        session_id: str,
        sections: dict[str, list[str]],
    ) -> HandoffData:
        resolved = self._guard_and_audit_memory_write(
            project_root,
            session_id,
            self.handoff_file(project_root, session_id),
            event_kind="session.handoff.updated",
            sections=sections,
        )
        # #363: credential scrub per value line (headers come from code).
        sections = {k: _scrub_credential_lines(v) for k, v in sections.items()}
        resolved.write_text(self._render_handoff_sections(sections), encoding="utf-8")
        return self.read_handoff(project_root, session_id)

    def _parse_handoff_steps(self, lines: list[str]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "-":
                continue
            match = re.match(
                r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+)\s+@\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\s+(.+)$",
                stripped,
            )
            if not match:
                legacy_match = re.match(r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+):\s+(.+)$", stripped)
                if legacy_match:
                    marker, step_id, text = legacy_match.groups()
                    result.append(
                        {
                            "id": step_id,
                            "status": self._normalize_handoff_step_marker(marker),
                            "text": text.strip(),
                            "updated_at": "",
                        },
                    )
                continue
            marker, step_id, updated_at, text = match.groups()
            result.append(
                {
                    "id": step_id,
                    "status": self._normalize_handoff_step_marker(marker),
                    "text": text.strip(),
                    "updated_at": updated_at,
                },
            )
        return result

    def _render_handoff_steps(self, steps: list[dict[str, str]]) -> list[str]:
        if not steps:
            return ["-"]
        lines = []
        for step in steps:
            marker = self.HANDOFF_STEP_MARKERS.get(
                self._normalize_handoff_step_status(step.get("status", "open")),
                "[ ]",
            )
            updated_at = str(step.get("updated_at") or "").strip()
            if updated_at:
                lines.append(f"- {marker} {step['id']} @ {updated_at}: {step['text']}")
            else:
                lines.append(f"- {marker} {step['id']}: {step['text']}")
        return lines

    def _extract_bullet_path(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped.startswith("-"):
            return None
        value = stripped[1:].strip().strip("`")
        return value or None

    def _normalize_step_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.strip()).casefold()

    def _normalize_handoff_step_status(self, status: str) -> str:
        return "completed" if status == "done" else status

    def _normalize_handoff_step_line(self, line: str) -> str | None:
        stripped = line.strip()
        if not stripped or stripped == "-":
            return None
        match = re.match(
            r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+)\s+@\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}):\s+(.+)$",
            stripped,
        )
        if match:
            marker, step_id, updated_at, text = match.groups()
            status = self._normalize_handoff_step_marker(marker)
            return self._render_handoff_steps(
                [{"id": step_id, "status": status, "text": text.strip(), "updated_at": updated_at}],
            )[0]
        legacy_match = re.match(r"^-\s+(\[[^\]]+\])\s+([A-Za-z0-9_-]+):\s+(.+)$", stripped)
        if legacy_match:
            marker, step_id, text = legacy_match.groups()
            status = self._normalize_handoff_step_marker(marker)
            return self._render_handoff_steps(
                [{"id": step_id, "status": status, "text": text.strip(), "updated_at": ""}],
            )[0]
        return None

    def _normalize_handoff_step_marker(self, marker: str) -> str:
        normalized = re.sub(r"\s+", "", marker).casefold()
        aliases = {
            "[]": "open",
            "[open]": "open",
            "[todo]": "open",
            "[x]": "completed",
            "[done]": "completed",
            "[complete]": "completed",
            "[completed]": "completed",
            "[~]": "reset",
            "[reset]": "reset",
            "[reopen]": "reset",
            "[re-open]": "reset",
            "[!]": "failed",
            "[failed]": "failed",
            "[fail]": "failed",
            "[?]": "stale",
            "[stale]": "stale",
        }
        return aliases.get(normalized, self.HANDOFF_MARKER_STATES.get(marker, "open"))

    def _parse_plan_checkbox_line(self, line: str) -> dict[str, str] | None:
        stripped = line.strip()
        match = re.match(r"^-\s+(\[[ xX~>!]\])\s+(.+)$", stripped)
        if not match:
            return None
        marker, text = match.groups()
        status = self.PLAN_MARKER_STATES.get(marker)
        if status is None:
            return None
        return {"status": status, "text": text.strip()}

    def _parse_lane_aware_steps(self, lines: list[str]) -> tuple[list[PlanPhase], list[PlanLane]]:
        phases: list[PlanPhase] = []
        lanes: list[PlanLane] = []
        current_phase: PlanPhase | None = None
        current_lane: PlanLane | None = None
        saw_lane_metadata = False
        phase_ids: set[str] = set()
        lane_ids: set[str] = set()

        def finalize_lane() -> None:
            nonlocal current_lane
            if current_lane is None:
                return
            if not current_lane.files:
                raise ValueError(f"Files are required for lane '{current_lane.name}'")
            lanes.append(current_lane)
            if current_phase is not None:
                current_phase.lanes.append(current_lane)
            current_lane = None

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped == "-":
                continue

            metadata = self._parse_lane_metadata_line(stripped)
            if metadata is not None:
                saw_lane_metadata = True
                kind, value = metadata
                if kind == "phase":
                    finalize_lane()
                    phase_id = self._slugify_plan_metadata(value, fallback="phase")
                    if phase_id in phase_ids:
                        raise ValueError(f"Duplicate phase id '{phase_id}' in lane-aware plan")
                    phase_ids.add(phase_id)
                    current_phase = PlanPhase(phase_id=phase_id, name=value)
                    phases.append(current_phase)
                    continue
                if kind == "lane":
                    if current_phase is None:
                        raise ValueError(
                            "Lane entries require a Phase before the first "
                            "Lane. Insert a bullet line `- Phase: <name>` "
                            "at the top of the Steps section — NOT a "
                            "heading like `### Phase 1` (those are "
                            "ignored). Every `- Lane:` entry must live "
                            "under a preceding `- Phase:` bullet.",
                        )
                    finalize_lane()
                    lane_id = self._slugify_plan_metadata(value, fallback="lane")
                    if lane_id in lane_ids:
                        raise ValueError(f"Duplicate lane id '{lane_id}' in lane-aware plan")
                    lane_ids.add(lane_id)
                    current_lane = PlanLane(
                        lane_id=lane_id,
                        phase_id=current_phase.phase_id,
                        name=value,
                    )
                    continue
                if kind == "files":
                    if current_lane is None:
                        raise ValueError("Files entries require an active Lane")
                    if current_lane.files:
                        raise ValueError(f"Files already declared for lane '{current_lane.name}'")
                    current_lane.files = self._parse_plan_metadata_list(value, field_name="Files")
                    continue
                if kind == "depends_on":
                    if current_lane is None:
                        raise ValueError("depends_on entries require an active Lane")
                    if current_lane.depends_on:
                        raise ValueError(
                            f"depends_on already declared for lane '{current_lane.name}'",
                        )
                    current_lane.depends_on = [
                        self._slugify_plan_metadata(d, fallback="dep")
                        for d in self._parse_plan_metadata_list(value, field_name="depends_on")
                    ]
                    continue
                if kind == "quality":
                    # Phoenix 2026-05-12 (#157): Quality field is now
                    # silently ignored (the worker/expert tier split
                    # is gone — workers ARE experts). Parsing kept
                    # so old plans don't error; the value is discarded.
                    continue
                if kind == "allowed_tools":
                    # Phoenix 2026-05-09: parse per-lane tool override.
                    # Comma-separated list; empty = no override (use config default).
                    if current_lane is None:
                        raise ValueError("Allowed tools entries require an active Lane")
                    if current_lane.allowed_tools:
                        raise ValueError(
                            f"Allowed tools already declared for lane '{current_lane.name}'",
                        )
                    current_lane.allowed_tools = self._parse_plan_metadata_list(
                        value,
                        field_name="Allowed tools",
                    )
                    continue
                if kind == "verification":
                    # Phoenix 2026-05-09: capture per-lane verification command.
                    if current_lane is None:
                        raise ValueError("Verification entries require an active Lane")
                    if current_lane.verification:
                        raise ValueError(
                            f"Verification already declared for lane '{current_lane.name}'",
                        )
                    current_lane.verification = value
                    continue

            parsed_step = self._parse_plan_checkbox_line(line)
            if parsed_step is not None and current_lane is not None:
                current_lane.steps.append(
                    PlanStep(status=parsed_step["status"], text=parsed_step["text"]),
                )

        finalize_lane()
        if not saw_lane_metadata:
            return [], []

        for lane in lanes:
            for dependency in lane.depends_on:
                if dependency == lane.lane_id:
                    raise ValueError(f"Lane '{lane.name}' cannot depend on itself")
                if dependency not in lane_ids:
                    raise ValueError(
                        f"Unknown depends_on lane id '{dependency}' for lane '{lane.name}'",
                    )

        return phases, lanes

    def _parse_plan_metadata_list(self, raw_value: str, field_name: str) -> list[str]:
        values = [item.strip() for item in raw_value.split(",") if item.strip()]
        if not values:
            raise ValueError(f"{field_name} must list at least one value")
        return values

    def _parse_lane_metadata_line(self, stripped_line: str) -> tuple[str, str] | None:
        patterns = (
            ("phase", r"^-\s+Phase:\s+(.+)$"),
            ("lane", r"^-\s+Lane:\s+(.+)$"),
            ("files", r"^-\s+Files:\s+(.+)$"),
            ("depends_on", r"^-\s+depends_on:\s+(.+)$"),
            # Phoenix 2026-05-12 (#157): Quality field deprecated.
            # Regex retained so old plans don't fail on the line;
            # the parser silently ignores the value (workers ARE
            # experts now — see #157).
            ("quality", r"^-\s+(?:Quality|quality):\s+(.+)$"),
            # Phoenix 2026-05-09: per-lane tool override + verification
            # command. Previously dropped on the floor (template said
            # "narrative; parser reads them as steps or ignores"). Now
            # they drive the runtime gate's lane_allowed_tools and the
            # packet's verification_commands.
            ("allowed_tools", r"^-\s+Allowed tools:\s+(.+)$"),
            ("verification", r"^-\s+Verification:\s+(.+)$"),
        )
        for kind, pattern in patterns:
            match = re.match(pattern, stripped_line)
            if match:
                return kind, match.group(1).strip()
        return None

    def _is_lane_metadata_line(self, stripped_line: str) -> bool:
        return self._parse_lane_metadata_line(stripped_line) is not None

    def _slugify_plan_metadata(self, value: str, fallback: str) -> str:
        # Short slug first (<=60 chars) so simple lane names stay
        # readable. Long briefs get content-addressed to a short hash
        # suffix — the full brief survives in PlanLane.name for
        # display, but the id is bounded so it can't blow Windows'
        # 32 KB argv limit when spawned through a CLI. The leading
        # slug fragment + hash keeps the id identifiable at a glance
        # while being audit-verifiable (recompute sha1 → same id).
        slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
        if not slug:
            return fallback
        if len(slug) <= 60:
            return slug
        import hashlib as _hashlib

        digest = _hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
        head = slug[:40].rstrip("-")
        return f"{head}-{digest}"

    def _normalize_plan_prose_text(self, text: str) -> str:
        normalized = text.strip().rstrip(".")
        normalized = re.sub(
            r"^(the\s+agent\s+should|agent\s+should|should)\s+",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return ""
        return normalized[0].upper() + normalized[1:]

    def _extract_code_paths_from_text(self, text: str) -> list[str]:
        matches: list[str] = []
        for candidate in re.findall(r"`([^`\n]+)`", text):
            if self._looks_like_code_path(candidate):
                matches.append(candidate)
        for candidate in re.findall(
            r"\b[\w./\\-]+\.(?:py|js|ts|jsx|tsx|cs|cshtml|csproj|sql|css|scss|json|yaml|yml|html|razor|go|rs|java|kt|rb|php|sh|ps1)\b",
            text,
        ):
            if self._looks_like_code_path(candidate):
                matches.append(candidate)
        return matches

    def _looks_like_code_path(self, value: str) -> bool:
        normalized = value.replace("\\", "/").strip()
        if normalized.startswith("/.MEMORY/"):
            return False
        return normalized.endswith(
            (
                ".py",
                ".js",
                ".ts",
                ".jsx",
                ".tsx",
                ".cs",
                ".cshtml",
                ".csproj",
                ".sql",
                ".css",
                ".scss",
                ".less",
                ".json",
                ".yaml",
                ".yml",
                ".html",
                ".razor",
                ".go",
                ".rs",
                ".java",
                ".kt",
                ".rb",
                ".php",
                ".sh",
                ".ps1",
                ".bash",
            ),
        )

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
        body = stripped[1:].strip().strip("`")
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
        return (
            f"- `{claim['agent_id']} | {claim['run_id']} | {claim['mode']} | {claim['last_seen']}`"
        )

    # ── Session journal ──────────────────────────────────────────────

    JOURNAL_FILENAME = "journal.md"

    @staticmethod
    def _journal_config(project_root: Path | None = None) -> tuple[int, int, set[str], int]:
        """Load journal config live via get_setting. Dashboard changes
        to journal.max_entries / journal.evict_batch / journal.
        trivial_actions / journal.min_intent_length take effect on the
        next write without MCP restart.
        """
        from .config import _parse_string_list, get_setting

        def _int(key: str, default: int) -> int:
            v = get_setting(key, project_root=project_root, default=default)
            try:
                return int(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        # max_entries: 0 = unlimited (never evict). min_intent: 0 = no
        # minimum (accept all). Read without the `or default` footgun that
        # silently turned a configured 0 back into the default.
        max_entries = _int("journal.max_entries", 100)
        evict_batch = int(
            get_setting(
                "journal.evict_batch",
                project_root=project_root,
                default=20,
            )
            or 20,
        )
        trivial = set(
            _parse_string_list(
                get_setting(
                    "journal.trivial_actions",
                    project_root=project_root,
                    default="",
                ),
            ),
        )
        min_intent = _int("journal.min_intent_length", 10)
        return max_entries, evict_batch, trivial, min_intent

    def journal_path(self, project_root: Path, session_id: str) -> Path:
        return self.session_path(project_root, session_id) / self.JOURNAL_FILENAME

    def journal_archive_path(self, project_root: Path, session_id: str) -> Path:
        return project_root / ".MEMORY" / "archive" / "sessions" / session_id / "journal-archive.md"

    def read_journal(
        self,
        project_root: Path,
        session_id: str,
        last_n: int | None = None,
    ) -> list[dict[str, str]]:
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
        max_ent, evict_batch, trivial_actions, min_intent_len = self._journal_config(project_root)

        # Significance filter — skip trivial actions
        if action_kind in trivial_actions:
            return {"logged": False, "evicted": 0, "reason": f"trivial action: {action_kind}"}

        # Skip very short intents (greetings, single-word commands)
        clean_intent = intent.strip()
        if len(clean_intent) < min_intent_len:
            return {"logged": False, "evicted": 0, "reason": "intent too short"}

        limit = max_entries if max_entries is not None else max_ent
        path = self.journal_path(project_root, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Read existing entries
        existing_lines: list[str] = []
        if path.is_file():
            existing_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

        # Evict if at capacity. limit 0 = unlimited (never evict).
        evicted = 0
        if limit > 0 and len(existing_lines) >= limit:
            evicted = self._evict_journal_entries(project_root, session_id, existing_lines)
            # Re-read after eviction
            existing_lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

        # Append new entry
        timestamp = self._timestamp()
        # #363: scrub credential-shaped spans BEFORE truncation — truncating
        # first could shear a secret into a fragment the guard no longer
        # recognizes, persisting a partial credential.
        safe_intent = _scrub_credential_text(clean_intent)
        safe_outcome = _scrub_credential_text(outcome.strip())
        # Truncate intent and outcome to keep entries concise
        short_intent = safe_intent[:120].replace("|", "/").replace("\n", " ")
        short_outcome = safe_outcome[:120].replace("|", "/").replace("\n", " ")
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
        _, evict_batch, _, _ = self._journal_config(project_root)
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
        # #363: journal-archive.md is git-tracked — rescrub on eviction so
        # entries written before the write-time guard existed cannot carry
        # credential-shaped content into the tracked archive.
        archive_block = "\n".join(_scrub_credential_lines(to_archive))
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

    def snapshot_prompt_submit_state(self, project_root: Path) -> dict[str, object]:
        """Capture session artifacts that auto-bind may create in phase T."""
        from .session_membership_store import SessionMembershipStore
        root = self.sessions_root(project_root)
        disk_ids = tuple(sorted(path.name for path in root.iterdir() if path.is_dir())) if root.is_dir() else ()
        members = tuple(sorted(SessionMembershipStore().list_members(project_root)))
        return {"captured": True, "existed": bool(disk_ids or members), "state": {"disk_ids": disk_ids, "members": members}}

    def restore_prompt_submit_state(
        self,
        project_root: Path,
        snapshot: dict[str, object],
        *,
        created_session_ids: tuple[str, ...] = (),
    ) -> None:
        """Compensate only auto-* sessions created by prompt auto-bind."""
        if snapshot.get("captured") is not True:
            raise RuntimeError("refusing session restore from uncaptured snapshot")
        state = snapshot.get("state")
        if not isinstance(state, dict):
            raise RuntimeError("session snapshot state is malformed")
        before = set(state.get("disk_ids") or ()) | set(state.get("members") or ())
        created = tuple(dict.fromkeys(str(item) for item in created_session_ids if item))
        unsafe = sorted(
            item for item in created
            if item in before or not item.startswith("auto-")
        )
        if unsafe:
            raise RuntimeError(
                "refusing unscoped session compensation: " + ", ".join(unsafe)
            )
        from .session_membership_store import SessionMembershipStore
        membership = SessionMembershipStore()
        for session_id in created:
            membership.unregister(project_root, session_id)
            path = self.session_path(project_root, session_id)
            if path.is_dir():
                shutil.rmtree(path)
