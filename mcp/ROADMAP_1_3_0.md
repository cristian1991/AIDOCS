# AIDOCS Roadmap (v1.3.0)

## Theme

Make cross-session and cross-project work meaningfully better, even without true host-native shared agent sessions.

This roadmap is about improving coordination, continuity, and shared operational context between sessions, projects, and agents.

Note: the first collaboration-continuity foundations (structured handoffs, resume bundles, initial freshness signals) were pulled forward into the real `v1.2.0` work because they proved more urgent than originally expected.

This roadmap now represents the **next layer after that foundation**.

It does **not** assume AIDOCS can become a full substitute for a real harness-native session model. Instead, it aims to make AIDOCS much better at preserving and transferring state between otherwise separate runs.

## Core Reality

AIDOCS already has pieces of the problem:

- project-local durable memory
- selected-session context
- session journal
- execution evidence
- related-project helpers
- indexes that make past work more searchable

But cross-session work is still weak in practice because:

- handoffs are too manual
- relevant state is spread across session files, journals, memory, indexes, and tool history
- cross-project linking is possible but not deeply integrated into everyday workflows
- agents can discover prior work, but they do not yet inherit a strong enough working context from it

## v1.3.0 End Goal

By the end of v1.3.0, AIDOCS should make multi-session and multi-project collaboration feel intentional instead of incidental.

The system still will not be a true shared harness session.

But it should be able to:

- reconstruct what happened recently
- explain why it happened
- show what still matters
- hand off active context cleanly to another session or project
- surface related work without broad manual digging

## What v1.3.0 Is Not

- not full shared memory between live agents
- not live cross-agent conversational state sync
- not replacement for host-native session management
- not a promise that all agents will behave identically across hosts

## Priority Snapshot

| Priority | Area | Goal |
|---|---|---|
| P0 | step-based handoff state | evolve handoffs from structured summaries into living step/state trackers |
| P0 | current-state reconstruction | answer “what is going on right now?” across sessions and projects with incremental resume logic |
| P1 | cross-project linking | improve how one project references active work in another |
| P1 | shared operator summaries | produce better summaries for humans and successor agents |
| P1 | task lifecycle enforcement | ensure session journals are populated even when agents skip explicit task_complete calls |
| P2 | workflow-assisted coordination | use workflow/execution signals to improve handoff quality |
| P3 | host-aware collaboration upgrades | prepare for future host/session APIs without depending on them |

## End Goals

### End Goal 1: Session Handoffs Become First-Class
- Handoffs are structured, not ad hoc markdown notes.
- Sessions can declare outgoing and incoming handoff state.
- Agents can quickly answer:
  - what was done
  - what remains
  - what is blocked
  - what files matter
  - what assumptions are active

### End Goal 2: Current-State Reconstruction Is Strong
- AIDOCS can reconstruct “current reality” from session state, journals, execution evidence, and indexes.
- Resuming work should not require manual reading across many files.
- The system should highlight stale versus fresh information.

### End Goal 3: Cross-Project Work Is Easier
- Related projects can carry stronger links than freeform references.
- Sessions can point to sibling sessions in other repos.
- Handoffs between projects can be represented explicitly.

### End Goal 4: Human and Agent Summaries Improve
- Operator-facing summaries become clearer and more concise.
- Successor agents receive richer, more targeted context bundles.
- “What should I know before touching this?” becomes easier to answer.

### End Goal 5: Collaboration Surfaces Stay Honest
- AIDOCS clearly states where it helps and where the host still owns session continuity.
- The product never implies true shared-session guarantees it cannot provide.

## Partial Goals

### Partial Goal A: Structured Session Handoff Model
- Evolve the existing handoff foundation into step-based collaboration state.
- Support changed/reset/open/failed semantics at the step level.
- Prefer incremental consumption over full rereads.

### Partial Goal B: Resume Bundles
- Extend existing resume bundles so they prioritize changed/open/reset work.
- Make resume consumption more incremental and less document-heavy.

### Partial Goal C: Staleness and Confidence Signals
- Mark handoff facts as fresh, stale, or uncertain.
- Show when a handoff predates recent file or execution changes.
- Reduce the risk of agents trusting old summaries blindly.

### Partial Goal D: Cross-Project Session Linking
- Let sessions reference external project sessions directly.
- Support “this session depends on that session” links.
- Improve related-project session discovery.

### Partial Goal E: Shared Operator Summaries
- Build operator-facing “state of work” summaries across one or more sessions.
- Make these useful for both humans and successor agents.

### Partial Goal F: Task Lifecycle Enforcement
- Track edit counts since last `task_complete` in PreToolUse hook.
- Escalate nudges: advisory → urgent → blocking based on configurable thresholds.
- Auto-journal on git commit and session release.
- Works on git and non-git projects via tool-call counting.

## Workstreams

## 1. Structured Handoff System

### Why
Current handoffs are helpful but too manual and inconsistent.

### Work
- extend the current handoff model into explicit step/state collaboration tracking
- support changed/reset/open/failed semantics at the step level
- let later agents read only newly changed or reopened work by default
- reduce the need to reread full handoff prose when only a few actionable steps changed

### Done when
- handoffs act like living collaboration state
- successor agents can focus on changed or reopened work instead of full-document rereads

## 2. Resume and Continuity Bundles

### Why
Sessions have context, but the best context is not packaged well enough for reuse.

### Work
- create richer resume/context bundle modes
- combine session state, journal, execution, and file relevance into one view
- support a “resume this session well” retrieval path
- prefer reading changed/open/reset handoff steps by default instead of always rereading the full handoff body
- support incremental handoff consumption for working and testing agent loops

### Done when
- resuming a session feels faster and less lossy
- agents get a coherent current-state package instead of many disconnected fragments
- agents can focus on newly changed or re-opened work without reprocessing the entire handoff every time

## 3. Cross-Project Linking

### Why
Real work often spans multiple repos, but current linking is too weak and too manual.

### Work
- let related projects expose active sessions in a queryable way
- support explicit cross-project handoff references
- build better compare/transfer context for sibling repos

### Done when
- cross-project work no longer depends on informal copy/paste memory alone

## 4. Staleness, Freshness, and Trust Signals

### Why
The system needs to help agents judge whether prior context is still trustworthy.

### Work
- timestamp and diff-aware freshness checks for handoffs and summaries
- show when linked files changed after a handoff was written
- surface uncertainty when relevant evidence is incomplete
- mark individual handoff steps as reset/stale when they need to be revisited
- let testing-agent feedback reopen previously completed handoff steps

### Done when
- agents and operators can see which context is current versus potentially outdated
- reset work is visible and actionable instead of disappearing into summary prose

## 5. Operator-Facing Collaboration Views

### Why
Humans need a better “what is the state across sessions?” view, not just raw session files.

### Work
- create session bundles focused on operator handoff/status review
- create current-work summaries across multiple active sessions
- make related-session and related-project state easier to inspect together

### Done when
- humans can quickly understand active collaboration state without broad manual reading

## 6. Workflow and Execution Assisted Handoffs

### Why
Execution evidence and workflow triggers should improve collaboration quality, not just auditing.

### Work
- connect execution summaries into handoff generation
- highlight incomplete workflow-triggered tasks in session summaries
- show what actually ran versus what was only planned

### Done when
- handoffs reflect reality, not just stated intent

## 7. Host-Aware Collaboration Readiness

### Why
Future hosts may expose better session APIs. AIDOCS should be ready without blocking on them.

### Work
- define collaboration contracts that host adapters can consume later
- keep collaboration state host-neutral where possible
- avoid coupling the design to one host’s current limitations

### Done when
- AIDOCS can adopt stronger host session capabilities later without redesigning the core collaboration model

## 8. Task Lifecycle Enforcement

### Why
Agents know `task_complete` exists but skip it during rapid iteration. This means session journals stay empty, handoff context is lost, and successor agents have no record of what happened. The task lifecycle is advisory-only — nothing enforces it.

### End Goal
Every meaningful piece of work gets logged to the session journal without requiring the agent to remember to call `task_complete` manually. Works for git and non-git projects alike.

### Partial Goal A: Edit-Count Tracking in PreToolUse Hook
- Track the number of edit/write tool calls since the last `task_complete`.
- After N edits without a `task_complete`, escalate the nudge:
  - 3 edits: advisory reminder
  - 6 edits: urgent reminder with summary of what changed
  - 10 edits: block further edits until `task_complete` is called
- Use execution evidence index (already tracks tool calls) as the data source.

### Partial Goal B: Auto-Journal on Significant Milestones
- When the agent calls git commit (detected via tool execution), auto-log the commit message to the session journal.
- When the agent calls `task_complete`, log the result summary (already implemented).
- When a session is released or archived, log a final summary entry.

### Partial Goal C: Non-Git Project Support
- Edit-count tracking works regardless of version control since it's based on tool call counts.
- Journal entries come from task lifecycle, not git history.
- Projects without git still get full session journal coverage.

### Work
- Add edit-count state to the PreToolUse hook (Claude) and tool.execute.after hook (OpenCode)
- Add configurable thresholds to `aidocs.toml` (e.g., `[task] nudge_after = 3, block_after = 10`)
- Add auto-journal on git commit detection
- Test with real multi-edit sessions to calibrate thresholds

### Done when
- Session journals are reliably populated even when agents skip explicit task lifecycle calls
- The enforcement is calibrated: not annoying for small edits, firm for substantial unreported work
- Works on both git and non-git projects

## Suggested New Surfaces

These are candidate additions for v1.3.0, not promises yet.

- `session_handoff_create`
- `session_handoff_get`
- `session_handoff_list`
- `session_resume_bundle`
- `session_state_summary`
- `related_project_session_list`
- `related_project_session_bundle`
- `collaboration_status_bundle`

## Release Criteria

Before shipping v1.3.0:

- structured handoff model exists
- resume/context bundles are materially stronger than raw session reads
- at least one cross-project handoff path is supported cleanly
- freshness/staleness signals exist for handoff-heavy flows
- docs explain clearly what AIDOCS collaboration can and cannot do

## Summary

v1.3.0 should make AIDOCS much better at collaboration continuity.

Not by pretending it is a true shared host session.

But by making session state, handoffs, resumption, and cross-project work strong enough that separate agent runs feel connected, intelligible, and much less lossy.
