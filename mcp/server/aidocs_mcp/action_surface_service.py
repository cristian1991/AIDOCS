from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class ActionSurfaceService:
    """Cross-layer comparison over procedure, capability, execution, and link surfaces."""

    _NOISY_FAMILIES = {"capability", "procedure", "execution", "project_status"}
    _PREFERRED_FAMILIES = {
        "memory",
        "task_lifecycle",
        "session",
        "bootstrap",
        "workflow",
        "schema",
        "code",
        "project",
        "related_project",
        "orchestration",
        "analysis",
    }

    def __init__(self, hub) -> None:
        self.hub = hub

    def compare(
        self,
        project_root: Path,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        needle = query.strip()
        if not needle:
            return {
                "query": query,
                "session_id": session_id,
                "should": [],
                "can": [],
                "did": {"events": [], "runs": []},
                "links": [],
                "coverage": {
                    "has_definition": False,
                    "has_capability": False,
                    "has_execution": False,
                    "resolved_link_count": 0,
                    "unresolved_link_count": 0,
                },
            }

        procedures = self.hub.procedures.find_procedures(project_root, query=needle, limit=limit)
        capabilities = self.hub.capabilities.find_capabilities(
            project_root,
            query=needle,
            limit=limit,
        )
        all_capabilities = self.hub.capabilities.find_capabilities(
            project_root,
            query=None,
            limit=1000,
        )
        events = self.hub.execution.list_events(
            project_root,
            query=needle,
            session_id=session_id,
            limit=limit,
        )
        runs = self._filter_runs(
            self.hub.execution.list_runs(
                project_root,
                session_id=session_id,
                limit=max(limit * 5, 50),
            ),
            needle=needle,
            limit=limit,
        )

        seen_links: dict[str, dict[str, Any]] = {}
        for procedure in procedures:
            procedure_id = str(procedure.get("procedure_id") or "").strip()
            if not procedure_id:
                continue
            for link in self.hub.procedure_links.list_links(
                project_root,
                procedure_id=procedure_id,
                limit=limit,
            ):
                seen_links.setdefault(str(link.get("link_id") or ""), link)
                capability_name = str(link.get("capability_name") or "").strip()
                if capability_name and not any(
                    item.get("name") == capability_name for item in capabilities
                ):
                    capability = self.hub.capabilities.get_capability(project_root, capability_name)
                    if capability is not None:
                        capabilities.append(capability)

        links = list(seen_links.values())[:limit]
        resolved_link_count = sum(
            1 for item in links if item.get("resolution_status") == "resolved"
        )
        unresolved_link_count = sum(
            1 for item in links if item.get("resolution_status") == "unresolved"
        )
        history_summary = self._build_history_summary(events, runs)
        gap_summary = self._build_gap_summary(procedures, capabilities, links, all_capabilities)
        assessment = self._build_assessment(
            has_definition=bool(procedures),
            has_capability=bool(capabilities),
            has_execution=bool(events or runs),
            resolved_link_count=resolved_link_count,
            unresolved_link_count=unresolved_link_count,
            gap_summary=gap_summary,
            history_summary=history_summary,
        )

        return {
            "query": needle,
            "coverage": {
                "definitions": len(procedures),
                "capabilities": len(capabilities),
                "events": len(events),
                "runs": len(runs),
                "links_resolved": resolved_link_count,
                "links_unresolved": unresolved_link_count,
            },
            "assessment": assessment,
            "history": history_summary,
            "gaps": gap_summary,
        }

    def assess(
        self,
        project_root: Path,
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        comparison = self.compare(project_root, query=query, session_id=session_id, limit=limit)
        assessment = (
            comparison.get("assessment") if isinstance(comparison.get("assessment"), dict) else {}
        )
        coverage = (
            comparison.get("coverage") if isinstance(comparison.get("coverage"), dict) else {}
        )
        history = (
            comparison.get("history_summary")
            if isinstance(comparison.get("history_summary"), dict)
            else {}
        )
        gaps = (
            comparison.get("gap_summary") if isinstance(comparison.get("gap_summary"), dict) else {}
        )
        state = str(assessment.get("state") or "unmapped")

        return {
            "query": comparison.get("query"),
            "state": state,
            "headline": self._headline_for_state(state),
            "findings": self._build_findings(coverage=coverage, history=history, gaps=gaps),
            "next_steps": list(assessment.get("recommended_next_steps") or []),
        }

    def status_bundle(
        self,
        project_root: Path,
        queries: list[str],
        session_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        normalized_queries = []
        seen = set()
        for item in queries:
            query = str(item).strip()
            if not query:
                continue
            lowered = query.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized_queries.append(query)

        assessments = [
            self.assess(project_root, query=query, session_id=session_id, limit=limit)
            for query in normalized_queries
        ]
        by_state: dict[str, int] = {}
        attention_items: list[dict[str, Any]] = []
        ready_items: list[dict[str, Any]] = []
        for assessment in assessments:
            state = str(assessment.get("state") or "unmapped")
            by_state[state] = by_state.get(state, 0) + 1
            gap_type = assessment.get("gap_type")
            item: dict[str, Any] = {
                "query": assessment.get("query"),
                "state": state,
                "headline": assessment.get("headline"),
                "next_steps": assessment.get("next_steps") or [],
            }
            if gap_type:
                item["gap_type"] = gap_type
            if state in {"aligned", "ready_but_unexecuted"}:
                ready_items.append(item)
            else:
                attention_items.append(item)

        overall_state = "healthy"
        if attention_items:
            if any(
                item.get("state") in {"definition_without_capability", "execution_only", "unmapped"}
                for item in attention_items
            ):
                overall_state = "attention_required"
            else:
                overall_state = "partial"

        return {
            "session_id": session_id,
            "queries": normalized_queries,
            "overall_state": overall_state,
            "counts": {
                "queries": len(normalized_queries),
                "attention_required": len(attention_items),
                "ready_or_aligned": len(ready_items),
                "by_state": by_state,
            },
            "attention_items": attention_items[:10],
            "ready_items": ready_items[:10],
        }

    def session_status_bundle(
        self,
        project_root: Path,
        session_id: str,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        queries = self._derive_session_queries(
            project_root,
            session_id=session_id,
            max_queries=max_queries,
        )
        bundle = self.status_bundle(
            project_root,
            queries=queries,
            session_id=session_id,
            limit=limit,
        )
        bundle["derived_queries"] = queries
        bundle["query_source"] = "session_context"
        return bundle

    def current_session_bundle(
        self,
        project_root: Path,
        limit: int = 20,
        max_queries: int = 12,
    ) -> dict[str, Any]:
        from .managed_mode_service import resolve_managed_session

        managed = self.hub.managed_mode.get_mode(project_root)
        # #1027 authority door decides WHICH SESSION this bundle reads; the
        # `managed` state above is kept only for the descriptive
        # managed_mode_active field stamped on the bundle below.
        session_id = resolve_managed_session(self.hub.managed_mode, project_root)
        resolution = "managed_mode"
        if not session_id:
            active = [
                item
                for item in self.hub.sessions.list_sessions(project_root)
                if item.status == "active"
            ]
            if len(active) == 1:
                session_id = active[0].session_id
                resolution = "single_active_session"
            else:
                return {
                    "ready": False,
                    "reason": "session_selection_required",
                    "resolution": "none",
                    "active_sessions": [
                        {
                            "session_id": item.session_id,
                            "title": item.title,
                            "status": item.status,
                            "owner": item.owner,
                            "goal": item.goal,
                        }
                        for item in active
                    ],
                }

        bundle = self.session_status_bundle(
            project_root,
            session_id=session_id,
            limit=limit,
            max_queries=max_queries,
        )
        bundle["ready"] = True
        bundle["resolution"] = resolution
        bundle["managed_mode_active"] = bool(managed.get("active"))
        return bundle

    def _filter_runs(
        self,
        runs: list[dict[str, Any]],
        needle: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        lower = needle.lower()
        matched = []
        for run in runs:
            haystacks = [
                str(run.get("run_kind") or ""),
                str(run.get("procedure_id") or ""),
                str(run.get("capability_name") or ""),
                str(run.get("target_entity") or ""),
                json.dumps(run.get("metadata") or {}, sort_keys=True, default=str),
            ]
            if any(lower in value.lower() for value in haystacks):
                matched.append(run)
            if len(matched) >= limit:
                break
        return matched

    def _build_history_summary(
        self,
        events: list[dict[str, Any]],
        runs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        latest_event = events[0] if events else None
        latest_run = runs[0] if runs else None
        capabilities_seen = sorted(
            {
                str(item.get("capability_name") or "").strip()
                for item in [*events, *runs]
                if str(item.get("capability_name") or "").strip()
            },
        )
        return {
            "event_count": len(events),
            "run_count": len(runs),
            "latest_event_kind": latest_event.get("event_kind") if latest_event else None,
            "latest_event_at": latest_event.get("observed_at") if latest_event else None,
            "latest_run_status": latest_run.get("status") if latest_run else None,
            "latest_run_at": latest_run.get("completed_at")
            if latest_run
            else (latest_run.get("started_at") if latest_run else None),
            "capabilities_seen": capabilities_seen,
        }

    def _build_gap_summary(
        self,
        procedures: list[dict[str, Any]],
        capabilities: list[dict[str, Any]],
        links: list[dict[str, Any]],
        all_capabilities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        unresolved_actions = sorted(
            {
                str(item.get("action_kind") or "").strip()
                for item in links
                if item.get("resolution_status") == "unresolved"
                and str(item.get("action_kind") or "").strip()
            },
        )
        return {
            "missing_definition": not bool(procedures),
            "missing_capability": not bool(capabilities),
            "unresolved_actions": unresolved_actions,
            "candidate_capabilities": {
                action: self._suggest_capabilities(action, all_capabilities)
                for action in unresolved_actions
            },
        }

    def _suggest_capabilities(
        self,
        action_kind: str,
        capabilities: list[dict[str, Any]],
    ) -> list[str]:
        tokens = {token for token in action_kind.lower().split("_") if token}
        scored: list[tuple[int, str]] = []
        for capability in capabilities:
            name = str(capability.get("name") or "").strip()
            if not name:
                continue
            family = str(capability.get("capability_family") or "").strip().lower()
            aliases = [
                str(item).strip().lower()
                for item in capability.get("aliases") or []
                if str(item).strip()
            ]
            score = 0
            if family and family in tokens:
                score += 3
            if any(
                alias in action_kind.lower() or action_kind.lower() in alias for alias in aliases
            ):
                score += 4
            if any(token and token in name.lower() for token in tokens):
                score += 1
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in scored[:5]]

    def _build_assessment(
        self,
        *,
        has_definition: bool,
        has_capability: bool,
        has_execution: bool,
        resolved_link_count: int,
        unresolved_link_count: int,
        gap_summary: dict[str, Any],
        history_summary: dict[str, Any],
    ) -> dict[str, Any]:
        state = "unmapped"
        if has_definition and has_capability and has_execution and resolved_link_count > 0:
            state = "aligned"
        elif has_definition and has_capability and resolved_link_count > 0:
            state = "ready_but_unexecuted"
        elif has_definition and has_capability:
            state = "partially_mapped"
        elif has_definition and not has_capability:
            state = "definition_without_capability"
        elif not has_definition and has_capability and has_execution:
            state = "execution_without_definition"
        elif has_capability and not has_execution:
            state = "capability_without_history"
        elif has_execution and not has_definition and not has_capability:
            state = "execution_only"

        recommended_next_steps: list[str] = []
        unresolved_actions = list(gap_summary.get("unresolved_actions") or [])
        if not has_definition:
            recommended_next_steps.append(
                "Define or capture the intended procedure/workflow for this action.",
            )
        if not has_capability and unresolved_actions:
            recommended_next_steps.append(
                "Add or declare a callable capability for the unresolved workflow action.",
            )
        if unresolved_actions:
            recommended_next_steps.append(
                "Review candidate capabilities before adding any new alias or direct mapping.",
            )
        if has_capability and not has_execution:
            recommended_next_steps.append(
                "Capture real execution evidence for this capability through MCP invocation history.",
            )
        if unresolved_link_count > 0 and not unresolved_actions:
            recommended_next_steps.append(
                "Inspect unresolved procedure-capability links and verify whether the gap is real or naming-related.",
            )
        if history_summary.get("run_count", 0) > 0 and resolved_link_count == 0 and has_capability:
            recommended_next_steps.append(
                "Link observed execution back to a procedure or explicitly accept it as ad-hoc work.",
            )
        if not recommended_next_steps:
            recommended_next_steps.append(
                "No immediate gap detected; continue monitoring execution history and drift.",
            )

        # Classify as architectural gap (not designed) vs implementation gap (designed but not built/used)
        gap_type = None
        if state == "definition_without_capability":
            gap_type = "implementation_gap"
        elif state == "execution_without_definition" or state == "execution_only":
            gap_type = "architectural_gap"
        elif state == "capability_without_history" or state == "ready_but_unexecuted":
            gap_type = "implementation_gap"
        elif state == "unmapped":
            gap_type = "architectural_gap"
        elif state == "partially_mapped":
            gap_type = "implementation_gap"

        result: dict[str, Any] = {
            "state": state,
            "next_steps": recommended_next_steps[:4],
        }
        if gap_type:
            result["gap_type"] = gap_type
        return result

    def _headline_for_state(self, state: str) -> str:
        mapping = {
            "aligned": "Fully aligned — designed, built, and used.",
            "ready_but_unexecuted": "Implementation gap — designed and built, but never used.",
            "partially_mapped": "Implementation gap — partially wired, needs completion.",
            "definition_without_capability": "Implementation gap — designed but not built yet.",
            "execution_without_definition": "Architectural gap — built and used, but not in the design spec.",
            "capability_without_history": "Implementation gap — built but never used.",
            "execution_only": "Architectural gap — used without design or declared capability.",
            "unmapped": "Architectural gap — not in the design spec yet.",
        }
        return mapping.get(state, "Needs review.")

    def _build_findings(
        self,
        *,
        coverage: dict[str, Any],
        history: dict[str, Any],
        gaps: dict[str, Any],
    ) -> list[str]:
        findings: list[str] = []
        if coverage.get("has_definition"):
            findings.append("Procedure definition exists.")
        else:
            findings.append("No procedure definition is currently mapped.")
        if coverage.get("has_capability"):
            findings.append("Callable capability exists.")
        else:
            findings.append("No callable capability matched this query.")
        if coverage.get("has_execution"):
            findings.append(
                f"Execution history exists ({history.get('run_count', 0)} runs / {history.get('event_count', 0)} events).",
            )
        else:
            findings.append("No execution history is currently recorded.")

        unresolved = list(gaps.get("unresolved_actions") or [])
        if unresolved:
            findings.append("Unresolved actions: " + ", ".join(unresolved[:5]) + ".")
        candidate_summary = (
            gaps.get("candidate_capabilities")
            if isinstance(gaps.get("candidate_capabilities"), dict)
            else {}
        )
        for action, candidates in list(candidate_summary.items())[:2]:
            if candidates:
                findings.append(f"Candidates for `{action}`: {', '.join(candidates[:3])}.")
        return findings[:6]

    def _derive_session_queries(
        self,
        project_root: Path,
        session_id: str,
        max_queries: int,
    ) -> list[str]:
        capabilities = self.hub.capabilities.find_capabilities(project_root, query=None, limit=1000)
        procedures = self.hub.procedures.find_procedures(project_root, query=None, limit=1000)
        runs = self.hub.execution.list_runs(project_root, session_id=session_id, limit=100)
        events = self.hub.execution.list_events(project_root, session_id=session_id, limit=100)
        session = self.hub.sessions.read_session(project_root, session_id)
        context = self.hub.sessions.read_context(project_root, session_id)

        ordered: list[str] = []
        seen: set[str] = set()
        family_by_name = {
            str(item.get("name") or "").strip(): str(item.get("capability_family") or "").strip()
            for item in capabilities
        }

        def add(value: str | None) -> None:
            item = str(value or "").strip()
            if not item:
                return
            lowered = item.lower()
            if lowered in seen:
                return
            seen.add(lowered)
            ordered.append(item)

        for item in procedures:
            if str(item.get("definition_kind") or "") == "workflow_action":
                add(str(item.get("action_kind") or "").strip() or None)
        for item in runs:
            capability_name = str(item.get("capability_name") or "").strip() or None
            if capability_name and family_by_name.get(capability_name, "") in self._NOISY_FAMILIES:
                continue
            add(capability_name)
        for item in events:
            capability_name = str(item.get("capability_name") or "").strip() or None
            if capability_name and family_by_name.get(capability_name, "") in self._NOISY_FAMILIES:
                continue
            add(capability_name)

        vocabulary = self._build_vocabulary(capabilities, procedures)
        session_lines = []
        for section_name in ("Goal", "Scope", "State", "Upcoming"):
            session_lines.extend(session.sections.get(section_name, []))
        for section_name in ("Relevant Commands", "Session Facts", "Constraints"):
            session_lines.extend(context.sections.get(section_name, []))
        for token in self._extract_candidate_tokens("\n".join(session_lines)):
            matched = vocabulary.get(token.lower())
            if matched:
                add(matched)
            elif "_" in token:
                add(token)

        preferred = [
            item
            for item in ordered
            if family_by_name.get(item, "") in self._PREFERRED_FAMILIES
            or family_by_name.get(item, "") == ""
        ]
        non_preferred = [item for item in ordered if item not in preferred]
        return (preferred + non_preferred)[:max_queries]

    def _build_vocabulary(
        self,
        capabilities: list[dict[str, Any]],
        procedures: list[dict[str, Any]],
    ) -> dict[str, str]:
        vocabulary: dict[str, str] = {}
        for capability in capabilities:
            name = str(capability.get("name") or "").strip()
            if name:
                vocabulary.setdefault(name.lower(), name)
            for alias in capability.get("aliases") or []:
                alias_text = str(alias).strip()
                if alias_text:
                    vocabulary.setdefault(alias_text.lower(), alias_text)
        for procedure in procedures:
            action_kind = str(procedure.get("action_kind") or "").strip()
            if action_kind:
                vocabulary.setdefault(action_kind.lower(), action_kind)
        return vocabulary

    def _extract_candidate_tokens(self, text: str) -> list[str]:
        candidates: list[str] = []
        candidates.extend(re.findall(r"`([^`]+)`", text))
        candidates.extend(re.findall(r"\b[a-z][a-z0-9_]{2,}\b", text.lower()))
        ordered: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(candidate).strip()
            lowered = normalized.lower()
            if not normalized or lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(normalized)
        return ordered
