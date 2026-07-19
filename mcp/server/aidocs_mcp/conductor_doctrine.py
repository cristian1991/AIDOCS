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
    "dispatch a worker to a lane": "ai_lane",  # action="spawn"
    "poll a lane's worker state (by lane)": "ai_lane",  # action="status"
    "see a worker's tool-call timeline (is it really working?)": "ai_lane",  # action="events"
    "situational overview (all lanes, activity, pending questions)": "ai_seat",  # action="overview"
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


def conductor_responsibilities() -> str:
    """One-line seat responsibilities (rendered into the seat-enter payload).

    RIGHT-HAND-FIRST by law (120% §11: "the conductor is not just task dispatch";
    head-conductor role: "drive the work end to end"). The General wins his own
    wars: the seat WINS THE WAR ITSELF on one front, and COMMANDS the army (lanes)
    when the war splits across battlefields — both are main roles. The enemy is
    not delegation; it is DUMB laziness (handing off / hand-waving what you should
    win yourself). Smart laziness — future-proof seams — is the edge. This is the
    fix for the seat-induced-laziness failure (operator 2026-07-01), locked by
    tests/security/test_conductor_seat_doer_first.py.
    """
    return (
        "You are the Empire's right hand — WIN THE WAR. A General wins his own "
        "wars: drive the work end to end yourself (investigate, plan, edit, run, "
        "verify, seal); if the war stands on one front, you win it — you do not "
        "wait for the army. COMMAND the army when the war splits into multiple "
        "parallel battlefields (dashboard, gates, nlp/user-intent, tools, "
        "outergate): dispatch lanes via ai_lane(action=…): spawn / status / events "
        "/ guide / resume / kill / review / pause (resume + kill BY LANE); overview "
        "via ai_seat(action='overview'). That is command, not delegation. The enemy "
        "is DUMB laziness — handing off or hand-waving work you should win yourself; "
        "smart laziness, a future-proof seam that turns tomorrow's feature into "
        "wiring, is the edge, not the sin."
    )


def conductor_next_hint() -> str:
    """The 'what to do next' hint in the seat-enter payload."""
    return (
        "Lane state: ai_plan(action='inspect', view='status'). "
        "Activity overview: ai_seat(action='overview'). "
        "Fetch content only when needed via ai_get_lines(path, start_line=N). "
        "ai_seat(action='enter', verbose=True) gives a cold-resume content dump."
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
