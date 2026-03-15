# Changelog

Completed work promoted by `/archive` lands here.

## Unreleased

### Added
- Added `/memstart` as the explicit startup/restart warm-up command for Claude and OpenCode.
- Added reusable memory-drift checker scripts, including arrow-key interactive navigation.
- Added project-root `/agents/` workspace conventions for spawned-agent artifacts.

### Changed
- Shifted session startup to `.aidocs/index.aidocs` -> `/.MEMORY/NOW.md` -> `/.MEMORY/INDEX.md`.
- Made `build/` the runtime/public AIDOCS root for project updates and setup.
- Hardened Claude auto-memory into redirect-only bootstrap behavior.
- Replaced `DONE.md`-style completion ledgers with project-root `CHANGELOG.md`; `/archive` now promotes completed items from `NOW.md` and daily logs into it.

### Internal
- Seeded this changelog from prior `NOW.md`, `DONE.md`, and daily-log completion history during memory-system cleanup.
