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
    "plan the work into lanes": "ai_plan_create",
    "check lane graph / runnable lanes": "ai_plan_status",
    "dispatch a worker to a lane": "ai_lane",  # action="spawn"
    "poll a lane's worker state (by lane)": "ai_lane",  # action="status"
    "see a worker's tool-call timeline (is it really working?)": "ai_lane",  # action="events"
    "situational overview (all lanes, activity, pending questions)": "ai_seat",  # action="overview"
    "nudge a RUNNING lane worker (it sees the msg on its next call)": "ai_lane",  # action="guide"
    "worker stalled / narrated-and-quit → resume its session (by lane)": "ai_lane",  # action="resume"
    "runaway or wrong-path worker → terminate (by lane)": "ai_lane",  # action="kill"
    "session frozen (SELF_MOD / strike) → break-glass clear": "admin_clear_freeze",
    "decide a pending lane completion review": "ai_lane",  # action="review"
    "pause / resume a lane": "ai_lane",  # action="pause" / action="control" state="active"
    "ask the operator a question": "ai_qa",  # action="ask"
    "message another seat (king / co-conductor)": "ai_msg",
}

# ── Toolset groups (all REAL names) ──────────────────────────────────
CONDUCTOR_TOOLSETS: dict[str, list[str]] = {
    # Lane control + incident response. Clause B folded the scattered verbs
    # (spawn/status/events/kill/resume/guide/review/pause) into ai_lane(action=…)
    # — the SINGLE conductor surface. ai_worker is the worker-id-level twin;
    # admin_clear_freeze is the break-glass freeze exit.
    "control": [
        "ai_lane",
        "ai_worker",
        "admin_clear_freeze",
    ],
    "planning": [
        "ai_plan_create",
        "ai_plan_status",
        "ai_plan_dispatch",
        "ai_plan_report",
        "ai_plan_graph",
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
    """One-line seat responsibilities (rendered into the seat-enter payload)."""
    return (
        "plan lanes (ai_plan_create), then drive everything through the single "
        "conductor surface ai_lane(action=…): spawn / status / events / guide / "
        "resume / kill / review / pause (resume + kill BY LANE). Overview: "
        "ai_seat(action='overview'). Inline work: just work — no need to dispatch lanes for "
        "single-file edits."
    )


def conductor_next_hint() -> str:
    """The 'what to do next' hint in the seat-enter payload."""
    return (
        "Lane state: ai_plan_status. Activity overview: ai_seat(action='overview'). "
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
