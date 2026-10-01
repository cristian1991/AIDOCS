---
description: Update changelog and archive completed work
command_id: archive
preferred_executor: advisory
allow_uninitialized: false
---
## Intent
- Archive completed work into canonical memory and `/.MEMORY/CHANGELOG.md`.

## Inputs
- Use `$ARGUMENTS` to override the default daily-log selection when provided.
- Otherwise use the last 7 days of daily files by default.

## Preconditions
- Use only the active target project's `/.MEMORY/`.

## Primary Execution
1. Read selected session files with status `done` under `/.MEMORY/sessions/*/SESSION.md`.
2. Read the selected `/.MEMORY/daily/*` files.
3. Promote durable signal into canonical targets under `rules/*`, `system/*`, `config/*`, and `domains/*`.
4. Create or update `/.MEMORY/CHANGELOG.md` from completed session summaries and completed items found in the selected daily logs.
5. Merge or edit existing memory files instead of creating duplicate append-only growth.
6. Use `Supersedes:` when replacing guidance.
7. Move processed daily files to `/.MEMORY/archive/`.

## Branching
- If legacy folders such as `policy`, `architecture`, `operations`, or `decisions` exist, merge forward and leave legacy files untouched unless the user explicitly asks for cleanup.
- If a candidate item is spam, a one-off, unverified, or secret-bearing, skip it.

## STOP Conditions
- Stop if the archive scope cannot be determined from the default rules and the provided arguments.
- Stop if a destructive or ambiguous archival action requires user choice.

## Output
- Report promoted items.
- Report changelog status.
- Report moved daily files.

## Fallback
- If the requested archive scope is ambiguous, ask for the exact date range or source set before continuing.

## Rules
- Do not archive or delete session folders during `/archive`.
- Sessions remain in `/.MEMORY/sessions/` until an explicit delete-session flow is used.
- Session-local plans remain with their session.
- Do not move session-local plans during `/archive`.
- Do not include secrets.
- Ignore spam, one-offs, and unverified claims.

## Arguments
$ARGUMENTS
