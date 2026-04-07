# Conductor Hardening Design

## Goal

Harden the AIDOCS conductor so it can reliably take single-project work to a trustworthy green state before attempting any cross-agent/cross-host protocol work.

## Core Problem

The conductor is implemented but not yet production-hardened. Current gaps:
- Lanes jump from local green to "done" without full-suite awareness
- No persistent lane ownership across reopen cycles
- Weak failure attribution
- Missing structured intra-project lane signaling

## Design Principles

1. **Full-suite awareness**: The conductor must know when lane-local green is not enough
2. **Persistent ownership**: Lane agents should remain owners across reopen cycles
3. **Deterministic attribution**: Failures should be attributed using runtime evidence
4. **Structured signaling**: Intra-project communication should be explicit, not implicit

## Lane Lifecycle States

- `blocked`
- `ready`
- `running`
- `awaiting_review`
- `implementation_done` (lane-local green)
- `reopened_by_integration` (full-suite or later phase attributed failure back to this lane)
- `awaiting_user_feedback`
- `completed` (only after full-suite verification passes)

## Full-Suite-Aware Verification

The conductor should:
1. Run lane-local verification
2. Run integration/full-suite verification where required
3. Inspect failures
4. Attribute likely ownership using runtime evidence
5. Reopen the owning lane automatically

## Persistent Lane Ownership

Lane agents should remain the owners of their lane across reopen cycles. The same lane agent can be reused later if:
- A downstream integration phase reveals a regression
- The conductor attributes that regression back to the lane

## Structured Intra-Project Lane Signals

Before 2.0.0, AIDOCS needs a small structured signal model for one project, enforced by the conductor:

- `hidden_dependency_found`
- `undeclared_file_needed`
- `waiting_on_contract`
- `integration_failure_reopened`
- `ownership_dispute`

The conductor should be the single inference point for these signals.

## Conductor Retrieval Model

The conductor should stay query-first:
- `code_find`
- `code_trace`
- `code_bundle`
- dependency/plan/session tools

It should avoid raw file reads except for exceptional confirmation cases.

## Success Criteria

Conductor hardening is complete when:
- Lane graph parsing works
- Unblocked lanes are computed correctly
- Overlapping-file lanes do not run in parallel
- Obvious in-flight conflicts can pause lanes
- One agent stays attached to one lane across sequential lane tasks
- Full-suite failures can reopen the owning lane automatically
- Lane ownership persists across reopen cycles
- User input can resume or restructure paused lanes
- Conductor-level coordination uses indexed/query tools more than raw file reads
