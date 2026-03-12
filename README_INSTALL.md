# AIDOCS Kit Install

Version: `AIDOCS-kit-v0.7.0` (2026-02-11)

## What this package contains
- Shared docs: `.aidocs/*`
- Memory templates: `.aidocs/templates/memory/**`
- Personality definitions: `.aidocs/personalities/*`
- OpenCode commands: `.opencode/command/*.md`
- Claude commands: `.claude/commands/*.md`
- Installer: `scripts/install-agent-routing.cmd` + `scripts/install-agent-routing.ps1`
- Utilities: `scripts/check-memory-drift.cmd` + `scripts/check-memory-drift.ps1`

## Install on another PC
1) Copy the `build/` folder (or the whole AIDOCS repo) anywhere.
2) Double-click `build/scripts/install-agent-routing.cmd`.

The install/runtime root is `build/`. Project init/update flows read shared setup files from that location.

Installer will create/update:
- `%USERPROFILE%\.config\opencode\AGENTS.md`
- `%USERPROFILE%\.config\opencode\commands\*.md` (all commands)
- `%USERPROFILE%\.claude\CLAUDE.md`
- `%USERPROFILE%\.claude\commands\*.md` (all commands)

## Notes
- Default remote creation visibility is private.
- Main memory entry point is project-root `/.MEMORY/INDEX.md`.
- Runtime task state is project-root `/.MEMORY/NOW.md`.
- Spawned-agent plans/investigations go in project-root `/agents/` with dated logical filenames.
- Use `scripts/check-memory-drift.cmd [path]` to scan a folder tree for memory routing/index drift.
- With no path, the checker opens an interactive arrow-key selector for drives and folders.
- Use `/memstart` at session start/restart when you want the agent to intentionally warm local `.aidocs` setup docs plus project memory entry files.
- `/memstart` warm-start order is: local `.aidocs/index.aidocs` -> `/.MEMORY/NOW.md` -> `/.MEMORY/INDEX.md`.
- Use `/project-init` to initialize a new project.
- Use `/project-update` to sync existing projects to latest.
- Use `/reingest` and choose `full-reingest` to populate memory from project docs.
- The installer reads from `build/` — run `scripts/sync-build.cmd` after editing root files to keep build current.
