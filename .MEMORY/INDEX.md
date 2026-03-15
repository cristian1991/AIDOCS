# Memory Index

Durable-memory router only. Not the session-start entry point.

Read order:
1. Read `/.MEMORY/.aidocs/index.aidocs` first.
2. Read `NOW.md` for active runtime state.
3. Only then use this file to open relevant durable-memory files.

## Adjacent
- [/.MEMORY/.aidocs/index.aidocs](.aidocs/index.aidocs) — session-start router
- [NOW.md](NOW.md) — runtime task state
- [CHANGELOG.md](CHANGELOG.md) — completed work history created by `/archive`

## Runtime
- (runtime state is in `NOW.md`; do not duplicate it here)

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
- [memory-system.md](domains/memory-system.md) — canonical memory entry/routing decisions and Claude enforcement rules

## Plans
(none yet — active implementation plans live here and are referenced from `NOW.md`)

## Daily
- [2026-03-05.md](daily/2026-03-05.md) — session log
- [2026-03-08.md](daily/2026-03-08.md) — session log
- [2026-03-12.md](daily/2026-03-12.md) — session log
- [2026-03-15.md](daily/2026-03-15.md) — session log

## Archive
(archived daily logs and completed plan files go here)
