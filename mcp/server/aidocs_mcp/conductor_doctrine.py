"""Canonical head-conductor doctrine — the SINGLE source of the conductor's
role text, situation→tool map, and toolset groups.

Why this module exists (120% §972 "docs say exactly what is true now"): the
conductor onboarding used to be inline string literals naming tools that did
not exist (`conductor_overview`, `conductor_lane_control`, `plan_dispatch_next`,
dead `task_begin`/`ai_str_replace`). A conductor reading its own role could not
find its tools. Lifting the doctrine here makes it (a) the ONE place both
`conductor_mode_enter` (seat payload) and `conductor_start` (persistent system
prompt) render from, and (b) ENFORCEABLE: `tests/security/
test_conductor_doctrine_tool_truth.py` asserts every name referenced here
resolves to a live tool on the real server surface, so a phantom can never ship
again.

Every tool name below is a REAL, agent-callable MCP tool. When the surface
consolidates (e.g. control verbs folding into `ai_lane(action=…)`), update the
maps here and the enforcement test keeps the role honest through the change.
"""

from __future__ import annotations

# ── Situation → the ONE tool that handles it ─────────────────────────
# This is the map a conductor reaches for under fire. Keys are the
# situation (human words); values are the exact live tool name.
SITUATION_TOOL_MAP: dict[str, str] = {
    "plan the work into lanes": "ai_plan",  # action="create"
    "check lane graph / runnable lanes": "ai_plan",  # action="inspect" view="status"
    # ── The THREE dispatch modes (operator directive 2026-07-25) ──
    # A conductor creates lanes one of exactly three ways. Only the plan
    # route used to be listed here, which is WHY the flow never surfaced
    # the other two: a conductor reading its own doctrine concluded that
    # dispatching a backlog item required authoring a plan document first.
    # MODE 3 — laned plan (multi-lane work with a graph + review gates):
    "dispatch a worker to a lane": "ai_lane",  # action="spawn" (after ai_plan action="create")
    # MODE 1 — backlog-redirect: THE way to put a worker on a filed war.
    # The item body is the brief and its own row takes the outcome, so
    # nothing is copied and nothing duplicates the war.
    "fix / dispatch an EXISTING backlog item #N": "ai_lane",  # action="delegate" lane_id="delegated-<N>"
    # MODE 2 — freetext: one ad-hoc task, no plan scaffold, no filed item.
    "fire ONE ad-hoc task at a worker (free-text brief)": "ai_lane",  # action="delegate" prompt="…"
    "poll a lane's worker state (by lane)": "ai_lane",  # action="status"
    "see a worker's tool-call timeline (is it really working?)": "ai_lane",  # action="events"
    "situational overview (all lanes, activity, pending questions)": "ai_seat",  # mode="overview"
    "nudge a RUNNING lane worker (it sees the msg on its next call)": "ai_lane",  # action="guide"
    "worker stalled / narrated-and-quit → resume its session (by lane)": "ai_lane",  # action="resume"
    "runaway or wrong-path worker → terminate (by lane)": "ai_lane",  # action="kill"
    # #386/#288 + Emperor challenge 2026-07-18: admin_clear_freeze is HIDDEN
    # operator break-glass (freeze-IMMUNE by operation_class — the OPERATOR's
    # exit, hidden ≠ disabled). This route serves the actor-scoped case (War V):
    # a frozen WORKER's conductor — a different, unfrozen actor — asks the
    # operator via the ai_qa decision rail. Under an OPERATOR-axis freeze
    # "nothing runs" (freeze_service): no agent tool works, the freeze card
    # itself carries the trail to the operator — stop, don't call.
    "session frozen (SELF_MOD / strike) → break-glass clear": "ai_qa",  # action="ask" (unfrozen seat asks; hard freeze: the card guides the operator)
    "decide a pending lane completion review": "ai_lane",  # action="review"
    "pause / resume a lane": "ai_lane",  # action="pause" / action="control" state="active"
    "ask the operator a question": "ai_qa",  # action="ask"
    "message another seat (Emperor / co-conductor)": "ai_msg",
}

# ── Toolset groups (all REAL names) ──────────────────────────────────
CONDUCTOR_TOOLSETS: dict[str, list[str]] = {
    # Lane control + incident response. Clause B folded the scattered verbs
    # (spawn/status/events/kill/resume/guide/review/pause) into ai_lane(action=…)
    # — the SINGLE conductor surface. ai_worker is the worker-id-level twin.
    # (admin_clear_freeze went surface=HIDDEN per #386/#288 — operator
    # break-glass, not an agent tool; a frozen conductor asks via ai_qa.)
    "control": [
        "ai_lane",
        "ai_worker",
    ],
    # #386/#359: the ai_plan_* standalones folded under ai_plan(action=…) —
    # one planning surface (create / inspect / dispatch / report / graph).
    "planning": [
        "ai_plan",
    ],
    "code": [
        "ai_find",
        "ai_investigate",
        "ai_trace",
        "ai_bundle",
        "ai_text_search",
        "ai_get_lines",
    ],
    "edit": [
        "ai_replace",
        "ai_batch_edit",
        "ai_create_file",
        "ai_insert_lines",
    ],
    "session": [
        "ai_task",
        "ai_seat",
        "ai_qa",
        "ai_msg",
    ],
}


def referenced_tool_names() -> set[str]:
    """Every tool name this doctrine references (map values + group members).
    The enforcement test asserts this set ⊆ the live server surface."""
    names: set[str] = set(SITUATION_TOOL_MAP.values())
    for group in CONDUCTOR_TOOLSETS.values():
        names.update(group)
    return names


# ── The ROLE BODY lives in the bundled scroll, never in code ─────────
# DRT-03: the head-conductor role text used to be a hardcoded string here,
# a stale operator row in empire SQL, AND a hand-written restatement in the
# persistent conductor_start prompt — three rival definitions of one seat.
# The role BODY is now the bundled role scroll (data/bundled_skills/
# head-conductor.md); this module keeps only what is legitimately code: the
# machine-maintained situation→tool map and toolset groups above.
_ROLE_SCROLL_ID = "head-conductor"
_CO_ROLE_SCROLL_ID = "co-conductor-doctrine"
_CO_ROLE_OPERATOR_ID = "co-conductor"


def bundled_role_body(skill_id: str) -> str:
    """The BODY of a bundled ROLE scroll — frontmatter stripped, whitespace
    normalized exactly the way ``SkillStore.read_role`` normalizes a registry
    row, so a bundled body and a registry row are directly comparable.

    Fails LOUD when the scroll is absent. There is deliberately no inline
    fallback: an inline copy is precisely the rival definition this fix
    exists to delete.
    """
    from .skill_provider import strip_frontmatter
    from .skill_store import SkillStore

    path = SkillStore._bundled_data_dir() / f"{skill_id}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"bundled role scroll missing: {path}. The role text has ONE home; "
            "the code carries no second copy to fall back to.",
        )
    return SkillStore._normalize_role_text(
        strip_frontmatter(path.read_text(encoding="utf-8")),
    )


def _registry_role_text(skills: object | None, skill_ids: tuple[str, ...]) -> str:
    """First non-empty operator/registry role row among ``skill_ids``, or ''.

    The registry row WINS when present (#479 operator-wins): an operator who
    inscribes a seat's role through the registry is making a motion, and the
    bundled payload only backfills absence.
    """
    resolved = skills
    if resolved is None:
        try:
            from .skill_store import SkillStore

            resolved = SkillStore()
        except Exception:
            return ""
    for sid in skill_ids:
        try:
            row = resolved.read_role(sid)  # type: ignore[attr-defined]
        except Exception:
            continue
        if row and row.get("content_text"):
            return str(row["content_text"])
    return ""


def conductor_responsibilities() -> str:
    """The canonical head-conductor role text: the BODY of the bundled
    `head-conductor` role scroll — the ONE home of that text.

    GENERAL-FIRST by law (head-conductor doctrine replacement, operator
    2026-09-01): the conductor owns the war but is not the army; agents are
    the default implementation force and direct editing is the exception.

    Locked by tests/security/test_conductor_seat_general_first.py (phrase
    pins) and tests/security/test_head_conductor_role_one_home.py (this
    function IS the scroll body, and nothing inlines a rival copy).
    """
    return bundled_role_body(_ROLE_SCROLL_ID)


def head_conductor_role_text(skills: object | None = None) -> str:
    """The ONE role text for the head-conductor seat: the registry row when
    an operator inscribed one, else the bundled scroll body. Callers emit the
    RESULT once — never this plus `conductor_responsibilities()`."""
    return _registry_role_text(skills, (_ROLE_SCROLL_ID,)) or conductor_responsibilities()


def co_conductor_role_text(skills: object | None = None) -> str:
    """Same precedence for the co-conductor seat: an operator-inscribed
    `co-conductor` row wins, else the shipped `co-conductor-doctrine` body
    (#501 — the id the seat actually delivers)."""
    return (
        _registry_role_text(skills, (_CO_ROLE_OPERATOR_ID, _CO_ROLE_SCROLL_ID))
        or bundled_role_body(_CO_ROLE_SCROLL_ID)
    )


def conductor_next_hint() -> str:
    """The 'what to do next' hint in the seat-enter payload."""
    return (
        "Lane state: ai_plan(action='inspect', view='status'). "
        "Activity overview: ai_seat(mode='overview'). "
        "Fetch content only when needed via ai_get_lines(path, start_line=N). "
        "ai_seat(mode='enter', verbose=True) gives a cold-resume content dump."
    )


def conductor_onboarding() -> list[str]:
    """The persistent-conductor system-prompt lines (TOOLS + incident map).
    Built from the structured maps so it can never drift into a phantom name.
    """
    lines: list[str] = ["== TOOLS =="]
    labels = {
        "control": "Lane control + incident response",
        "planning": "Planning",
        "code": "Code discovery (read)",
        "edit": "Edit (gated)",
        "session": "Session / identity",
    }
    for key, group in CONDUCTOR_TOOLSETS.items():
        lines.append(f"{labels.get(key, key)}: {', '.join(group)}")
    lines.append("")
    lines.append("== WHEN X HAPPENS, USE Y ==")
    for situation, tool in SITUATION_TOOL_MAP.items():
        lines.append(f"- {situation}: {tool}")
    return lines
