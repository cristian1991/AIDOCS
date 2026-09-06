from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session_query_gate_store import SessionQueryGateStore


# ── Plan-mode session lifecycle ──────────────────────────────────────────
# Module-level functions so the phrase-handler layer can call them without
# constructing a RuntimePlanAuthoringService (which needs a full runtime
# hub). The hook fires before the runtime is fully booted in some flows.

_PLAN_MODE_SCOPE_MIN = 3


def plan_session_enter(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    *,
    scope: str,
) -> dict[str, Any]:
    """Enter plan-mode for a session with a non-empty scope.

    Defends in depth against empty/whitespace scope — the phrase detector
    enforces scope_min_chars at parse time, but direct callers (MCP tool,
    tests) must not be able to silently flip plan-mode-on with no scope.
    """
    cleaned = (scope or "").strip()
    if len(cleaned) < _PLAN_MODE_SCOPE_MIN:
        return {
            "active": False,
            "session_id": session_id,
            "error": (
                f"plan_session_enter requires scope >= "
                f"{_PLAN_MODE_SCOPE_MIN} chars; got {len(cleaned)}."
            ),
        }
    store.init_db(project_root)
    store.set_plan_mode_state(project_root, session_id, active=True, scope=cleaned)
    state = store.get_plan_mode_state(project_root, session_id)
    return {
        "active": True,
        "session_id": session_id,
        "scope": cleaned,
        "started_at": state["started_at"],
    }


def plan_session_exit(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
    *,
    force: bool = False,
    justification: str = "",
) -> dict[str, Any]:
    """Exit plan-mode. force+justification reserved for validator-bypass
    in later phases; current phase always exits cleanly when called.
    """
    store.init_db(project_root)
    state = store.get_plan_mode_state(project_root, session_id)
    was_active = state["active"]
    store.set_plan_mode_state(project_root, session_id, active=False, scope=None)
    return {
        "ok": True,
        "session_id": session_id,
        "was_active": was_active,
        "force": force,
        "justification": justification,
    }


def plan_session_status(
    store: SessionQueryGateStore,
    project_root: Path,
    session_id: str,
) -> dict[str, Any]:
    """Read the plan-mode state. Sessions with no row return inactive."""
    store.init_db(project_root)
    state = store.get_plan_mode_state(project_root, session_id)
    return {
        "session_id": session_id,
        "active": state["active"],
        "scope": state["scope"],
        "started_at": state["started_at"],
        "last_activity_at": state["last_activity_at"],
    }


class RuntimePlanAuthoringService:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.hub = runtime.hub

    def _parse_spec_task_line(self, text: str) -> dict[str, object] | None:
        stripped = text.strip()
        if not stripped.casefold().startswith("task:"):
            return None
        parts = [part.strip() for part in stripped.split("|") if part.strip()]
        task_text = parts[0][5:].strip()
        if not task_text:
            return None
        task: dict[str, object] = {"text": task_text, "files": [], "depends_on": []}
        for part in parts[1:]:
            lowered = part.casefold()
            if lowered.startswith("files:"):
                task["files"] = [
                    item.strip() for item in part.split(":", 1)[1].split(",") if item.strip()
                ]
            elif lowered.startswith("depends_on:"):
                task["depends_on"] = [
                    item.strip() for item in part.split(":", 1)[1].split(",") if item.strip()
                ]
        return task

    def _spec_to_plan_sections(
        self,
        project_root: Path,
        session_id: str,
        spec_text: str,
        scope: str | None,
        constraints: list[str] | None,
    ) -> dict[str, list[str]]:
        session = self.hub.sessions.read_session(project_root, session_id)
        lines = [line.rstrip() for line in str(spec_text or "").splitlines()]
        purpose: str | None = None
        end_goal: str | None = None
        explicit_scope = scope.strip() if isinstance(scope, str) and scope.strip() else None
        validation: list[str] = []
        parsed_constraints: list[str] = list(constraints or [])
        task_defs: list[dict[str, object]] = []
        free_steps: list[str] = []
        current_section: str | None = None

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                continue
            lowered = stripped.casefold()
            if lowered.startswith("purpose:"):
                purpose = stripped.split(":", 1)[1].strip()
                current_section = None
                continue
            if lowered.startswith("goal:") or lowered.startswith("end goal:"):
                end_goal = stripped.split(":", 1)[1].strip()
                current_section = None
                continue
            if lowered.startswith("scope:"):
                explicit_scope = stripped.split(":", 1)[1].strip() or explicit_scope
                current_section = None
                continue
            if lowered == "validation:" or lowered.startswith("validation:"):
                tail = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                current_section = "validation"
                if tail:
                    validation.append(tail)
                continue
            if lowered == "constraints:" or lowered.startswith("constraints:"):
                tail = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
                current_section = "constraints"
                if tail:
                    parsed_constraints.append(tail)
                continue
            task = self._parse_spec_task_line(stripped)
            if task is not None:
                task_defs.append(task)
                current_section = None
                continue
            bullet = stripped[1:].strip() if stripped.startswith("-") else stripped
            if current_section == "validation":
                validation.append(bullet)
            elif current_section == "constraints":
                parsed_constraints.append(bullet)
            else:
                free_steps.append(bullet)

        session_goal = self.runtime._clean_bullets(session.sections.get("Goal", []))
        resolved_purpose = purpose or (
            session_goal[0] if session_goal else "Implement the requested work"
        )
        resolved_end_goal = end_goal or (session_goal[0] if session_goal else resolved_purpose)
        resolved_scope = (
            explicit_scope
            or self.runtime._clean_bullet_value(session.sections.get("Scope", []))
            or "-"
        )

        steps_lines: list[str] = []
        lane_ready_tasks = [task for task in task_defs if task.get("files")]
        if lane_ready_tasks:
            steps_lines.append("- Phase: Planned Work")
            for task in lane_ready_tasks:
                task_name = str(task.get("text") or "Task").strip()
                files = [str(item) for item in (task.get("files") or []) if str(item).strip()]
                depends_on = [
                    str(item) for item in (task.get("depends_on") or []) if str(item).strip()
                ]
                steps_lines.append(f"- Lane: {task_name}")
                steps_lines.append(f"- Files: {', '.join(files)}")
                if depends_on:
                    steps_lines.append(f"- depends_on: {', '.join(depends_on)}")
                steps_lines.append(f"- [ ] {task_name}")
        else:
            normalized_steps = free_steps or [
                str(task.get("text") or "").strip() for task in task_defs
            ]
            for step in normalized_steps:
                cleaned = str(step).strip()
                if cleaned:
                    steps_lines.append(f"- [ ] {cleaned}")

        if not validation:
            validation = ["Run the proving command(s) for this spec and record the results here."]

        return {
            "Purpose": [f"- {resolved_purpose}"],
            "Scope": [f"- {resolved_scope}"],
            "Current State": ["- Plan created from spec; implementation has not started yet."],
            "Partial Goals": [f"- {item}" for item in free_steps[:3]]
            or ["- Convert the spec into executable work."],
            "Steps": steps_lines or ["- [ ] Define executable implementation steps"],
            "End Goal": [f"- {resolved_end_goal}"],
            "Constraints": self.runtime._as_bullets(parsed_constraints)
            if parsed_constraints
            else ["- Keep work aligned with the provided spec and session scope."],
            "Validation": self.runtime._as_bullets(validation),
            "Next Steps": [
                "- Run plan_validate and plan_preflight before starting implementation.",
            ],
        }

    def plan_create_from_spec(
        self,
        project_root: Path,
        session_id: str,
        spec_text: str,
        scope: str | None = None,
        constraints: list[str] | None = None,
    ) -> dict[str, object]:
        # Passthrough path for already-authored lane-aware plan
        # markdown (2026-04-20). If the spec is already a complete
        # plan file — starts with "# Plan" and carries any of the
        # canonical lane-aware section headers — write it verbatim
        # so custom sections (## Why / ## Goal / ## Out of scope /
        # ## Lane graph / etc.) survive. The line-parser path below
        # only produces legacy PLAN_SECTION_ORDER shape and would
        # drop everything else.
        text = str(spec_text or "").lstrip()
        if text.startswith("# Plan") and any(
            marker in text
            for marker in (
                "## Why this beat exists",
                "## Lane graph",
                "## Out of scope",
                "## Sequence",
                "## Backlog inbox",
            )
        ):
            plan_path = self.hub.sessions.plan_file(project_root, session_id)
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            to_write = spec_text if spec_text.endswith("\n") else spec_text + "\n"
            # #778: BACK UP BEFORE REPLACING. This call is named `create` and
            # REPLACES -- by design (one session plan per session), but the
            # design was the only thing between an operator and permanent loss:
            # a live plan went 1091 lines -> 52 with the receipt reading
            # {"ok": true}, and `git show HEAD` could not recover the
            # uncommitted working delta. A timestamped sibling (not a single
            # .bak) because the loss that prompted this happened TWICE in five
            # minutes -- a fixed name would have had the second overwrite
            # destroy the backup of the first.
            import time as _time

            prior_text = ""
            backup_path = ""
            if plan_path.is_file():
                try:
                    prior_text = plan_path.read_text(encoding="utf-8")
                except OSError:
                    prior_text = ""
                if prior_text:
                    candidate = plan_path.parent / (
                        plan_path.name + f".bak-{_time.strftime('%Y%m%d-%H%M%S')}"
                    )
                    try:
                        candidate.write_text(prior_text, encoding="utf-8")
                        backup_path = str(candidate)
                    except OSError:
                        # A backup we cannot write must not stop the operation
                        # the caller asked for -- but the receipt below will
                        # then honestly carry backup="" rather than imply one.
                        backup_path = ""
            plan_path.write_text(to_write, encoding="utf-8")
            plan = self.hub.sessions.read_plan(project_root, session_id)
            # #214: the 0-lanes trap. A spec that declares lane INTENT
            # (`## Lane graph` / `### Lane N` headings) but parses to 0 lanes is
            # a silent degrade — the lane parser registers lanes ONLY from
            # `- Phase:` / `- Lane:` / `- Files:` bullets, so heading-style lane
            # graphs yield 0 lanes and ai_lane spawn later refuses ("plan has no
            # conductor lanes"). Refuse up front and name the template (mirrors
            # the raw-tool gate) instead of writing a 0-lane plan. Scoped to
            # lane-intent specs so a genuinely lane-less narrative plan still
            # saves.
            _lane_intent = "## Lane graph" in text or "### Lane" in text
            if not plan.lanes and _lane_intent:
                # #778: PUT IT BACK. This guard runs AFTER the write above, so
                # until now the tool returned ok:false OVER THE WRECKAGE of the
                # very file it was refusing to damage -- a refusal that has
                # already done the damage is not a refusal. Restore what was
                # there; if nothing was, remove the file this refused call
                # created, so a refused create leaves the tree as it found it.
                _restored = False
                try:
                    if prior_text:
                        plan_path.write_text(prior_text, encoding="utf-8")
                        _restored = True
                    else:
                        plan_path.unlink(missing_ok=True)
                except OSError:
                    _restored = False
                return {
                    "ok": False,
                    "session_id": session_id,
                    "path": str(plan_path),
                    "lane_count": 0,
                    "has_lanes": False,
                    "write_mode": "verbatim_lane_aware",
                    "restored": _restored,
                    "backup": backup_path,
                    "error": (
                        "Plan saved 0 conductor lanes. The Lane graph declares "
                        "lanes with `### Lane N` HEADINGS, which the parser "
                        "IGNORES — lanes register only from `- Phase:` / `- Lane:` "
                        "/ `- Files:` bullets. Call `ai_plan(action='create', transition='template')` for the strict "
                        "lane format, re-author with bullet lanes, then retry "
                        "ai_plan(action='create')."
                    ),
                }
            # #778: an honest receipt. "ok": true alone read as "nothing was
            # lost", which is exactly how a 1091-line plan disappeared without
            # anyone noticing until much later. `replaced` is only ever set when
            # a file really was overwritten, so it cannot become noise an
            # operator learns to ignore.
            receipt = {
                "ok": True,
                "session_id": session_id,
                "path": str(plan_path),
                "lane_count": len(plan.lanes),
                "has_lanes": bool(plan.lanes),
                "write_mode": "verbatim_lane_aware",
            }
            if prior_text:
                receipt["replaced"] = True
                receipt["replaced_lines"] = len(prior_text.splitlines())
                receipt["new_lines"] = len(to_write.splitlines())
                receipt["backup"] = backup_path
            return receipt

        patch = self._spec_to_plan_sections(
            project_root,
            session_id,
            spec_text=spec_text,
            scope=scope,
            constraints=constraints,
        )
        plan = self.hub.sessions.update_plan(project_root, session_id, patch)
        return {
            "ok": True,
            "session_id": session_id,
            "path": str(plan.path),
            "lane_count": len(plan.lanes),
            "has_lanes": bool(plan.lanes),
            "write_mode": "legacy_line_parser",
        }

    def plan_validate(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        plan = self.hub.sessions.read_plan(project_root, session_id)
        errors: list[str] = []
        warnings: list[str] = []
        steps: list[str] = []
        vague_patterns = self.runtime._plan_validation_vague_patterns(
            project_root=project_root,
            session_id=session_id,
        )
        for lines in plan.sections.values():
            for line in lines:
                parsed = self.runtime._parse_plan_checkbox_line(line)
                if not parsed:
                    continue
                text = str(parsed["text"]).strip()
                if text:
                    steps.append(text)
                    if any(pattern in text.casefold() for pattern in vague_patterns):
                        errors.append(f"Vague step: {text}")

        if not steps:
            errors.append("Plan has no executable checkbox steps.")

        validation = self.runtime._clean_bullets(plan.sections.get("Validation", []))
        if (
            not validation
            or validation
            == ["Define concrete verification steps before considering the work complete."]
            or validation
            == ["Run the proving command(s) for this spec and record the results here."]
        ):
            errors.append("Plan validation section must contain concrete verification commands.")

        if not plan.lanes:
            warnings.append(
                "Plan is not lane-aware; conductor will run inline or serial work only.",
            )

        return {
            "session_id": session_id,
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "step_count": len(steps),
            "has_lanes": bool(plan.lanes),
            "lane_count": len(plan.lanes),
        }

    def plan_preflight(
        self,
        project_root: Path,
        session_id: str,
    ) -> dict[str, object]:
        """Analyze a session plan and surface all decision points BEFORE implementation.

        Reads PLAN.md, extracts incomplete steps, runs ai_investigate on each,
        and returns: what exists, what's missing, what decisions the agent must make
        before starting. The agent resolves decisions once upfront, then implements
        without mid-plan stops.
        """
        plan = self.hub.sessions.read_plan(project_root, session_id)
        if not plan or not plan.sections:
            return {
                "session_id": session_id,
                "error": "No plan found for this session.",
            }

        # Extract incomplete steps from all plan sections
        steps: list[str] = []
        for section_name, lines in plan.sections.items():
            for line in lines:
                parsed = self.runtime._parse_plan_checkbox_line(line)
                if parsed and parsed["status"] != "completed":
                    steps.append(str(parsed["text"]))

        if not steps:
            return {
                "session_id": session_id,
                "steps": [],
                "message": "All plan steps are complete.",
            }

        # Investigate each step — find what exists, what's missing
        step_analysis: list[dict[str, object]] = []
        for step_text in steps:
            # Extract key concepts from the step text (first 3 significant words)
            words = [w for w in step_text.split() if len(w) > 3 and w[0].isalpha()]
            concept = " ".join(words[:3]) if words else step_text[:40]

            investigation = self.hub.code.investigate(project_root, concept, limit=3)
            findings = investigation.get("findings", [])
            next_tools = investigation.get("next_tools", [])

            # Classify: does infrastructure exist or is this greenfield?
            has_symbols = any(f.get("area") == "symbols" for f in findings)
            has_schema = any(
                f.get("area") in ("schema_entities", "schema_fields") for f in findings
            )
            has_files = any(f.get("area") == "files" for f in findings)

            if has_symbols or has_schema:
                status = "extend"  # modify existing code
            elif has_files:
                status = "integrate"  # wire into existing structure
            else:
                status = "create"  # greenfield, needs decisions

            decisions: list[str] = []
            if status == "create":
                decisions.append(
                    f"No existing code found for '{concept}' — decide: where to create, which patterns to follow",
                )
            if has_schema and not has_symbols:
                decisions.append(
                    f"Schema exists for '{concept}' but no service/controller code — decide: service layer architecture",
                )
            if not has_schema and has_symbols:
                decisions.append(
                    f"Code exists for '{concept}' but no schema — decide: is DB/model layer needed?",
                )

            step_analysis.append(
                {
                    "step": step_text,
                    "status": status,
                    "concept": concept,
                    "existing": investigation.get("summary", ""),
                    **({"decisions": decisions} if decisions else {}),
                    **({"next_tools": next_tools[:2]} if next_tools else {}),
                },
            )

        # Summarize decision points across all steps
        all_decisions = []
        for sa in step_analysis:
            for d in sa.get("decisions", []):
                all_decisions.append(d)

        create_steps = [sa for sa in step_analysis if sa["status"] == "create"]
        extend_steps = [sa for sa in step_analysis if sa["status"] == "extend"]
        integrate_steps = [sa for sa in step_analysis if sa["status"] == "integrate"]

        return {
            "session_id": session_id,
            "total_steps": len(steps),
            "steps": step_analysis,
            "summary": {
                "create": len(create_steps),
                "extend": len(extend_steps),
                "integrate": len(integrate_steps),
                "decisions_needed": len(all_decisions),
            },
            **({"decisions": all_decisions} if all_decisions else {}),
            "recommended_order": (
                "Resolve all decisions first, then implement 'extend' steps (safest), "
                "then 'integrate' steps, then 'create' steps (most risk)."
            ),
        }
