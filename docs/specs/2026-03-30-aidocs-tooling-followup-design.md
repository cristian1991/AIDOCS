# AIDOCS Tooling Follow-Up Design

## Goal

Address the remaining AIDOCS tooling issues discovered during host-hook implementation while preserving intentional collaboration behavior:

- per-file edit + reindex stays in place
- indexes should remain fresh during handled edits
- stale state should primarily represent unhandled drift
- plan and roadmap behavior should become deterministic and user-confirmed

Strict exact-span indexing is deferred to `2.0.0`.

## Scope

This pass covers:

- plan resolution fallback behavior
- roadmap fallback and update behavior
- `pending_user_feedback` / `awaiting_feedback` workflow
- handoff step lifecycle support
- explicit freshness metadata and semantics
- file creation/edit ergonomics
- exact-path line-read usability
- `mcp_server --help` correctness

This pass does not cover:

- first-class AIDOCS skills subsystem
- strict exact-span indexing for all languages

## Decision Flow For Plans And Roadmaps

### Session plan precedence

If the active session has `plans/PLAN.md`, AIDOCS loads and uses it.

### No session plan

If the session does not have a local plan, AIDOCS builds a merged work summary from:

- project roadmap items from `ROADMAP_2_0_0.md` or fallback roadmap files already recognized by AIDOCS
- session-local open work such as handoff steps and unresolved session state

In this case AIDOCS must not silently choose the next step. It should summarize the remaining work and ask the user what to work on.

### No plan and no roadmap

If there is no usable session plan and no usable roadmap/open-work source, AIDOCS should ask the user for the next steps and treat plan or roadmap creation as the next required action.

## Roadmap Model

Roadmap state is stored directly in `ROADMAP_2_0_0.md`.

AIDOCS may only mutate clearly recognizable actionable bullets. Narrative prose and descriptive paragraphs must remain untouched.

### Roadmap step states

- `- [ ]` open
- `- [~]` in progress
- `- [>]` pending user feedback
- `- [x]` completed
- `- [!]` blocked

### Completion feedback gate

When a session plan sequence appears complete, the matching roadmap item must not be auto-completed.

Instead:

1. mark the roadmap item as `pending user feedback`
2. ask the user to confirm completion or provide follow-up feedback
3. if the user confirms, move the roadmap item to `completed`
4. if the user requests more fixes, move it back to `in progress`

This gate blocks automatic advancement of that roadmap item, but it does not freeze all other work in the session.

## Plan Normalization For Prose-Only Additions

If AIDOCS reads a session plan and finds prose-only user additions that do not match the expected structured checklist shape:

1. preserve the original prose
2. propose a normalized structured representation
3. add the normalized representation into the plan marked as `awaiting feedback`
4. ask the user to confirm or revise it
5. only after approval should the prose be cleaned up or replaced

### Plan step states

- `- [ ]` open
- `- [~]` in progress
- `- [>]` awaiting feedback
- `- [x]` completed
- `- [!]` blocked

No fake structure should be silently inferred and treated as active work.

## Freshness Model

### Intended behavior

Freshness should remain aggressive for handled edits so that work becomes visible quickly to other agents working in the same project.

The intended collaboration behavior is:

- edit file
- replace that file's index entries
- keep overall index fresh

### Required semantics

- handled edits must not force full project rebuilds
- handled edits must replace stale rows for the touched file, not append duplicates
- execution-event writes must not accidentally mark code indexes fresh or stale
- stale should mainly mean unhandled drift or missing index state

### Status surface

Index status tools should expose explicit freshness metadata, including at least:

- freshness state such as `ready`, `missing`, or `stale`
- last sync timestamps where available
- source-of-drift reasons when stale is detected
- whether freshness was preserved by handled per-file replacement sync

## File Editing And Reading Ergonomics

### File creation

AIDOCS should support a native file-create path rather than requiring non-AIDOCS file creation for normal workflows.

### Existing-file editing

Keep line-based editing for existing files because it is deterministic and reviewable.

### Exact-path line reads

If the file path is already known exactly, AIDOCS should make line reads easier without requiring unnecessary discovery steps. Indexed discovery should remain the preferred path for search, but not a mandatory burden for obvious exact-path reads.

## Handoff Step Lifecycle

Handoff steps should support a real completed state in the tool layer, not just an implicit or alternate marker.

Tooling should accept a completed/done path consistently and render it deterministically.

## CLI Help

`python -m aidocs_mcp.mcp_server --help` must show help and exit cleanly.

It must not start the server as a side effect of asking for help.

## Exact-Span Indexing Deferral

Strict exact-line span indexing is deferred to `2.0.0`.

Reason:

- it is larger than this pass
- strict-only semantics are correct, but implementation spans multiple extractor backends
- AIDOCS should not emit approximate start/end ranges

When it is implemented later, strict exact spans should be emitted only when the extractor backend can guarantee them. Otherwise AIDOCS should return anchor lines only.

## Validation Requirements

This follow-up pass must include exhaustive tests for:

- missing session plan fallback
- roadmap summary fallback
- no-plan/no-roadmap user-prompt behavior
- roadmap `pending user feedback` transitions
- plan `awaiting feedback` transitions for prose-only additions
- handoff step completion status handling
- per-file replacement sync freshness behavior
- exact-path read behavior
- native file creation flow
- `mcp_server --help` behavior

The implementation is only complete when targeted tests for each subsystem pass and the full relevant suite passes.
