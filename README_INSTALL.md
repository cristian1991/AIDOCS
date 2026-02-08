# AIDOCS Kit Install

Version: `AIDOCS-kit-v0.5.0` (2026-02-09)

## What this package contains
- Root routers: `AIDOCS.md`, `AGENTS.md`, `CLAUDE.md`
- Shared docs: `.aidocs/*`
- Memory templates: `.aidocs/templates/memory/**`
- OpenCode commands: `.opencode/command/project.md`, `.opencode/command/archive.md`
- Claude commands: `.claude/commands/project.md`, `.claude/commands/archive.md`
- Installer: `scripts/install-agent-routing.cmd` + `scripts/install-agent-routing.ps1`

## Install on another PC
1) Copy `AIDOCS` folder anywhere.
2) Ensure the folder contains `AIDOCS.md` at its root.
3) Run `scripts/install-agent-routing.cmd`.

Installer will create/update:
- `%USERPROFILE%\.config\opencode\AGENTS.md`
- `%USERPROFILE%\.config\opencode\commands\project.md`
- `%USERPROFILE%\.config\opencode\commands\archive.md`
- `%USERPROFILE%\.claude\CLAUDE.md`
- `%USERPROFILE%\.claude\commands\project.md`
- `%USERPROFILE%\.claude\commands\archive.md`

## Notes
- Default remote creation visibility is private.
- Routers are link/list only (no forced full-doc imports).
- Runtime memory path is project-root `/.MEMORY/`.
