# Tiered Read Access Design

## Goal

Reduce friction from the current query-before-read model without weakening the real safety boundaries.

The current gate is too uniform. It treats all reads as equally risky, even when the agent already knows the exact target file or is operating inside a conductor-owned lane.

## Core Principle

The real safety boundary is not “every read must follow a query.”

The real safety boundary is:

- uncontrolled discovery
- uncontrolled scope expansion
- access to protected/sensitive paths

So AIDOCS should gate those aggressively, while making legitimate narrow reads easier.

## Read Tiers

### 1. Discovery reads

These stay strict.

Use when:

- file/path is unknown
- subsystem is unknown
- the agent is exploring broadly
- the goal is to find where something lives

These should still require indexed/query-first behavior such as:

- `code_find`
- `code_trace`
- `code_bundle`

## 2. Exact known reads

These should be lightweight.

Use when:

- the exact relative path is already known
- the agent wants a narrow, targeted read
- the request is not acting as disguised broad discovery

Allowed if:

- path is inside project root
- path is not in the protected security set
- read is narrow enough
- no wildcards or broad expansion are involved

This should not require a prior query token just to read one known file.

## 3. Lane-owned reads

These should be the easiest reads.

If the conductor has assigned a lane and a file is declared in that lane’s `Files`, then the lane agent should automatically be allowed to read it without repeated query ceremony.

This should be lane-scoped and deterministic.

## 4. Protected reads

These remain blocked or strongly restricted.

No bypass for:

- security config
- GUI/control-plane security settings
- secrets/credentials
- hardcoded protected paths

## Conductor Integration

When a lane starts, the conductor should grant lane-scoped read access to the declared `Files` for that lane.

If the lane agent needs another file outside the declared scope, it should raise a structured signal such as:

- `undeclared_file_needed`

Then the conductor decides whether to:

- allow the extra file
- reassign lane scope
- pause the lane
- ask the user

## Why This Is Better

This model:

- preserves strict safety for discovery
- reduces friction for exact known reads
- reduces repeated overhead for lane-owned work
- keeps protected paths protected

It is simpler and more principled than continuing to add ad hoc exceptions to the current gate.

## Non-Goals

This design does not allow:

- broad wildcard reads without discovery
- lane agents silently expanding scope on their own
- relaxing security-protected paths

## Success Criteria

This design is successful when:

- discovery still requires indexed/query-first behavior
- exact-path reads are easier for known safe targets
- conductor-owned lanes do not suffer repeated read friction for declared files
- tests clearly distinguish discovery, exact-known, lane-owned, and protected read behavior
