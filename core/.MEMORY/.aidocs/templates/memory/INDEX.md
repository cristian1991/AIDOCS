# Memory Index

Durable-memory router only. Not the session-start entry point.

Read order:
1. Read `/.MEMORY/.aidocs/index.aidocs` first.
2. Inspect active sessions under `sessions/*/SESSION.md`.
3. Read the selected session's `SESSION.md`.
4. Only then use this file to open relevant durable-memory files.

## Adjacent
- [/.MEMORY/.aidocs/index.aidocs](.aidocs/index.aidocs) — session-start router
- [CHANGELOG.md](CHANGELOG.md) — completed work history created by `/archive`

## Sessions
- `sessions/` — active per-session runtime state
- `archive/sessions/` — completed session history

## Rules
- [standards.md](rules/standards.md) — coding conventions, engineering rules
- [security.md](rules/security.md) — security invariants
- [workflow.md](rules/workflow.md) — process rules, priorities, preferences

## System
- [architecture.md](system/architecture.md) — how the system is built, boundaries, data model
- [caveats.md](system/caveats.md) — known pitfalls, gotchas, workarounds
- [testing.md](system/testing.md) — test procedures, user-specified scenarios

## Config
- [personality.md](config/personality.md) — active personality config

## Related Projects
- [FIXES_BY_OTHER_AGENTS.md](related-projects/FIXES_BY_OTHER_AGENTS.md) — cross-project issue/fix handoff log

## Domains
(none yet — topic-specific knowledge + decisions go here)

## Daily
(recent session logs go here)

## Archive
(archived daily logs and explicitly archived sessions go here)

<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->
