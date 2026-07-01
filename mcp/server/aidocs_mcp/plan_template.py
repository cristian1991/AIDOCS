"""Lane-aware plan template text + accessor.

Conductor agents call `plan_template_get()` to receive the canonical
markdown for a lane-aware plan file. The template is the single source
of truth for how lane graphs are shaped — fields align with what
plan_create_from_spec / plan_conductor_status / plan_dispatch_next
actually consume.

Workers must not author plans. The MCP tool that exposes this refuses
when AIDOCS_EXPERT_LANE_ID is set in the worker's env.
"""

from __future__ import annotations

import os

LANE_AWARE_PLAN_TEMPLATE = """# Plan — <title>

## Why this beat exists
<1–2 paragraphs. Problem statement + what landing this unlocks.>

## Goal
- <Single-sentence concrete outcome. "Land X so Y can Z.">

## Out of scope
- <explicit lines of things adjacent callers might assume are in>

## Steps

SYNTAX (strict — parser is pedantic):
  * `- Phase: <name>` — a bullet line, NOT a heading. Every lane lives
    under a phase. First line of Steps MUST be `- Phase: ...`.
  * `- Lane: <slug>` — starts a lane under the current phase.
  * `  - Files: path1, path2` — comma-separated list of files this
    lane is allowed to write. ENFORCED scope.
  * `  - depends_on: lane-slug-a, lane-slug-b` — other lane slugs
    that must be `done` before this one dispatches.
  * `  - [ ] <step description>` — checkbox steps inside a lane.

Do NOT use `### Phase N` or `## Phase` — those are headings the parser
ignores. The `- Phase:` bullet is what matters.

Example (copy shape, rename to real values):

- Phase: Tenant Fan-Out
- Lane: lane-a-cashflow
  - Files: src/Infrastructure/Services/CashFlowService.cs, tests/FooTests.cs
  - Verification: dotnet build src/Infrastructure exits 0
  - [ ] Audit all public methods
  - [ ] Port tenant-assertion
  - [ ] Build clean confirmed
- Lane: lane-b-prescription
  - Files: src/Infrastructure/Services/PrescriptionService.cs
  - depends_on: lane-a-cashflow
  - Verification: dotnet build src/Infrastructure exits 0
  - [ ] Apply pattern
  - [ ] Build clean confirmed

Optional fields (narrative; parser reads them as steps or ignores):
  Allowed tools, Eager grants, Requires, Contract in, Contract out

## Sequence

1. Dispatch <entry lane>, block until done.
2. When <entry lane> is done, dispatch <parallel lanes> in parallel
   (disjoint file sets, no cross-edit risk).
3. All lanes must finish before the beat is complete.

## State
- <currently-open lane(s), or "not started">

## Upcoming
- <post-beat follow-ups: integration docs, next beat's entry condition>

## Blockers
- <hard blocks: needs operator decision, external dep, etc.>

## Backlog inbox
- [ ] <ideas surfaced mid-beat that don't belong to any declared lane>

## Last Updated
- <ISO date>
"""


LANE_AWARE_PLAN_FIELD_NOTES = """Field notes (agent-facing):

Files: ENFORCED scope. Lane agents' writes outside this list are
  refused by access_gate. Keep disjoint across parallel lanes or the
  conductor cannot dispatch them concurrently.

Allowed tools: optional override of the default worker MCP tool set.
  Narrow for read-only audit lanes, widen for lanes that need eager
  deferred tools. Omit for the default set.

Eager grants: names of deferred MCP tools to pre-grant for this lane's
  worker so the agent calls them without a ToolSearch round-trip.
  Useful when a lane's domain is known (e.g. a "schema migration"
  lane wants `schema_query`, `schema_index_sync` eager).

Requires: lane dependency graph. plan_conductor_status refuses to
  dispatch a lane whose `requires` aren't all `done`. Entry lanes
  declare `Requires: (none)`.

Contract in / Contract out: TEXT for humans. Prevents signature drift
  mid-beat — if lane A changes the shape of what lane B relies on,
  operator catches it during plan review.

Verification: shell command (typically pytest) whose exit 0 marks
  the lane `done`. No green verify = no done, no dispatch of
  dependents.

Checkboxes: per-lane progress. planning_step_mark toggles them when
  sub-step granularity is useful for the operator.

Storage: .MEMORY/sessions/<session-id>/plans/<beat-name>.md.
  plan_create_from_spec parses this and builds the sqlite lane graph.
"""


def render_plan_template() -> str:
    """Return the lane-aware plan template with accompanying field notes."""
    return LANE_AWARE_PLAN_TEMPLATE + "\n\n---\n\n" + LANE_AWARE_PLAN_FIELD_NOTES


# ── Test fixture — canonical lane-aware plan used by tests ──
#
# Tests consume these constants instead of hard-coding expected strings
# so a template/schema regression is caught at the deepest layer. When
# the template shape changes, update this fixture (single source of
# truth) and every test picks up the new shape automatically.

LANE_AWARE_PLAN_FIXTURE_SECTIONS: dict[str, list[str]] = {
    "Why this beat exists": [
        "Ship the feature safely so we can retire the legacy path.",
    ],
    "Goal": [
        "- Land feature-X so downstream consumers can migrate.",
    ],
    "Out of scope": [
        "- Legacy-path cleanup (separate beat).",
    ],
    "Steps": [
        "- Phase: Planned Work",
        "- Lane: lane-a-core",
        "  - Files: src/core.py, tests/test_core.py",
        "  - Verification: python -m pytest tests/test_core.py exits 0",
        "  - [ ] Implement core behavior",
        "  - [ ] Land green tests",
        "- Lane: lane-b-adapter",
        "  - Files: src/adapter.py",
        "  - depends_on: lane-a-core",
        "  - Verification: python -m pytest tests/test_adapter.py exits 0",
        "  - [ ] Wire adapter",
        "  - [ ] Land green tests",
    ],
    "Sequence": [
        "1. Dispatch lane-a-core, block until done.",
        "2. When lane-a-core is done, dispatch lane-b-adapter.",
        "3. All lanes finished -> beat complete.",
    ],
    "State": ["- not started"],
    "Upcoming": ["- Integration doc refresh."],
    "Blockers": ["- (none)"],
    "Backlog inbox": ["- [ ] Track unrelated ideas here."],
    "Last Updated": ["- 2026-04-20"],
}


def lane_aware_plan_fixture_markdown(title: str = "Fixture Beat") -> str:
    """Render LANE_AWARE_PLAN_FIXTURE_SECTIONS as a valid plan markdown file.

    Tests consume this to build a plan on disk, then assert against
    LANE_AWARE_PLAN_FIXTURE_SECTIONS (the same dict). Any regression in
    the parser -> renderer round-trip surfaces as a test failure.
    """
    lines = [f"# Plan — {title}", ""]
    for section, body in LANE_AWARE_PLAN_FIXTURE_SECTIONS.items():
        lines.append(f"## {section}")
        for entry in body:
            lines.append(entry)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def conductor_only_refusal_reason() -> str | None:
    """Return a refusal string when the caller is a worker process,
    else None for conductor-level callers.

    Workers must NOT author plans — plans describe lane dispatch and
    a worker authoring its own lane graph is either a bug or an
    attempt to break out of isolation. AIDOCS_EXPERT_LANE_ID in env
    is the authoritative signal.
    """
    if os.environ.get("AIDOCS_EXPERT_LANE_ID", "").strip():
        return (
            "plan_template_get is conductor-only. Worker processes "
            "cannot author plans. Report your lane's output back to "
            "the conductor; it owns plan authoring and dispatch."
        )
    return None
