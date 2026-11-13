"""Cross-project operations — project_list, project_list_sessions, handoff_create.

Spec: conductors and sub-agents need to see every AIDOCS-enabled project
and write handoffs into them without switching the current session binding.
These run on top of the existing ProjectRegistryService + SessionStore +
per-project handoff machinery.

Eagerly exposed as MCP tools so the conductor never has to discover them
via ToolSearch — handoff is a common enough operation that latency matters.

Security note: today the handoff writes treat paths as text. Any later
attempt to ACTUALLY read those paths goes through the managed-mode sensitive-
file gate, which catches .env / credentials regardless of which project
they live in. Per-project security union (project B's stricter rules
applying to project A's reads into B) is deferred to tranche 2 because
per-project security config doesn't exist yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .project_registry_service import ProjectRegistryService
from .session_store import SessionStore


def _templates_root_for(project_root: Path) -> Path:
    # SessionStore needs a templates_root; the cross-project tools don't
    # create sessions (only list / write handoffs into existing ones), so
    # pointing at the target project's own .aidocs/ is fine — it's read
    # by existing SessionStore paths and harmless if absent.
    return project_root / ".MEMORY" / ".aidocs"


def project_list(*, include_session_counts: bool = False) -> dict[str, Any]:
    """Return every AIDOCS-enabled project the MCP has seen.

    Registry is populated by normal MCP usage — every project_root we act
    on gets recorded. include_session_counts adds a per-project session
    count via SessionStore.list_sessions for dashboard/briefing UX.
    """
    registry = ProjectRegistryService()
    projects = registry.list_projects()

    if include_session_counts:
        enriched: list[dict[str, Any]] = []
        for item in projects:
            root_str = str(item.get("project_root") or "")
            if not root_str:
                enriched.append(dict(item))
                continue
            root = Path(root_str)
            try:
                store = SessionStore(templates_root=_templates_root_for(root))
                sessions = store.list_sessions(root)
                count = len(sessions)
            except Exception:
                count = 0
            enriched.append({**item, "session_count": count})
        projects = enriched

    return {"ok": True, "projects": projects}


def project_list_sessions(project_root: str) -> dict[str, Any]:
    """List sessions for a named project. Target project need not be the
    currently-bound one — this is the primary cross-project discovery
    hook for handoff flows.
    """
    if not project_root or not str(project_root).strip():
        return {"ok": False, "error": "project_root is required"}

    root = Path(project_root)
    if not root.is_dir():
        return {
            "ok": False,
            "error": f"Project directory not found: {project_root}",
        }
    if not (root / ".MEMORY").is_dir():
        return {
            "ok": False,
            "error": (
                f"Not an AIDOCS project (no .MEMORY/ under {project_root}). "
                f"Only AIDOCS-enabled projects support cross-project session "
                f"listing."
            ),
        }

    try:
        store = SessionStore(templates_root=_templates_root_for(root))
        sessions = store.list_sessions(root)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to list sessions: {exc}",
        }

    result: dict[str, Any] = {
        "ok": True,
        "project_root": str(root),
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.title,
                "status": s.status,
                "owner": s.owner,
                "goal": s.goal,
                "last_updated": s.last_updated,
            }
            for s in sessions
        ],
    }
    # Observability (NOT authority): enumeration is membership-backed. If
    # on-disk SESSION.md dirs exist that the registry doesn't list AND the
    # legacy-ingest marker is absent, these are unmigrated legacy sessions —
    # surface a remediation hint rather than a silent, misleading empty/short
    # list. The on-disk scan never affects which sessions are returned.
    try:
        from .session_membership_store import SessionMembershipStore

        _ms = SessionMembershipStore()
        member_ids = set(_ms.list_members(root))
        disk_ids = set(_ms.on_disk_session_ids(root))
        unregistered = sorted(disk_ids - member_ids)
        if unregistered and not _ms.is_sealed(root):
            result["unmigrated_legacy_sessions"] = unregistered
            result["hint"] = (
                f"{len(unregistered)} on-disk session(s) are not in the "
                f"membership registry (legacy, predating it): "
                f"{', '.join(unregistered)}. Run `aidocs "
                f"migrate-control-authority` (or session_connect, which "
                f"self-heals once) to register them. File presence is not "
                f"authority; this is a hint only."
            )
    except Exception:
        pass
    return result


def handoff_create(
    *,
    target_project_root: str,
    target_session_id: str,
    purpose: list[str] | None = None,
    current_state: list[str] | None = None,
    what_was_done: list[str] | None = None,
    what_failed: list[str] | None = None,
    what_matters_now: list[str] | None = None,
    open_questions: list[str] | None = None,
    risks_and_blockers: list[str] | None = None,
    relevant_files: list[str] | None = None,
    estimated_effort: list[str] | None = None,
    suggested_next_steps: list[str] | None = None,
    related_sessions: list[str] | None = None,
    related_project_links: list[str] | None = None,
    freshness: list[str] | None = None,
    append: bool = False,
) -> dict[str, Any]:
    """Write a handoff into another project's session.

    Resolves target_project_root + target_session_id, validates both
    exist as an AIDOCS project + session, then writes through the same
    SessionStore.update_handoff path the per-project tool uses. The
    caller's current project binding is unaffected.
    """
    if not target_project_root or not str(target_project_root).strip():
        return {"ok": False, "error": "target_project_root is required"}
    if not target_session_id or not str(target_session_id).strip():
        return {"ok": False, "error": "target_session_id is required"}

    root = Path(target_project_root)
    if not root.is_dir():
        return {
            "ok": False,
            "error": f"Target project directory not found: {target_project_root}",
        }
    if not (root / ".MEMORY").is_dir():
        return {
            "ok": False,
            "error": (f"Not an AIDOCS project: {target_project_root} has no .MEMORY/"),
        }

    session_dir = root / ".MEMORY" / "sessions" / target_session_id
    if not session_dir.is_dir():
        return {
            "ok": False,
            "error": (
                f"Session not found: {target_session_id} does not exist in {target_project_root}"
            ),
        }

    # Build the section patch using the same shape session_handoff_update uses.
    def _bullets(items: list[str] | None) -> list[str] | None:
        if items is None:
            return None
        out: list[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            out.append(f"- {text}" if not text.startswith("-") else text)
        return out

    patch: dict[str, list[str]] = {}
    section_map = {
        "Purpose": purpose,
        "Current State": current_state,
        "What Was Done": what_was_done,
        "What Failed / Dead Ends": what_failed,
        "What Matters Now": what_matters_now,
        "Open Questions": open_questions,
        "Risks and Blockers": risks_and_blockers,
        "Relevant Files": relevant_files,
        "Estimated Effort": estimated_effort,
        "Suggested Next Steps": suggested_next_steps,
        "Related Sessions": related_sessions,
        "Related Project Links": related_project_links,
        "Freshness": freshness,
    }
    for section_name, value in section_map.items():
        bullets = _bullets(value)
        if bullets is not None:
            patch[section_name] = bullets

    if not patch:
        return {
            "ok": False,
            "error": (
                "Nothing to write — at least one handoff section must be "
                "provided (purpose, what_was_done, suggested_next_steps, etc.)"
            ),
        }

    try:
        store = SessionStore(templates_root=_templates_root_for(root))
        handoff = store.update_handoff(root, target_session_id, patch, append=append)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to write handoff: {exc}",
        }

    return {
        "ok": True,
        "target_project_root": str(root),
        "target_session_id": target_session_id,
        "handoff_path": str(handoff.path),
        "sections_written": sorted(patch.keys()),
    }
