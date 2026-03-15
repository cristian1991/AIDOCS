# AIDOCS

A portable toolkit for AI coding agents. Provides persistent memory, structured commands, and consistent behavior across projects.

This file is internal project documentation — agents do not need to read it. Agent entry points are `AGENTS.md` and `CLAUDE.md`, which route to `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, and `/.MEMORY/INDEX.md`.

## Architecture

```
.claude/commands/           → Claude Code command files
.opencode/command/          → OpenCode command files
scripts/                    → Installer, drift checker, and build sync scripts

build/                      → Distributable kit and runtime/public root (synced from root)
  .MEMORY/.aidocs/          → Reusable memory-system contracts and templates only
  .claude/commands/         → Copy of Claude commands
  .opencode/command/        → Copy of OpenCode commands
  scripts/                  → Installer and utility scripts
  AGENTS.md, CLAUDE.md      → External router templates
  opencode.jsonc            → OpenCode config
  RELEASE_ROOT.md           → Generated source-vs-release separation marker

/.MEMORY/                   → AIDOCS repo's own fully encapsulated project memory system
  .aidocs/                  → Session-start docs and system contracts
    index.aidocs            → Session-start documentation router
    global-instructions.*   → Agent behavior rules
    coding-standards.*      → Code quality rules
    memory-system.*         → Memory protocol
    project-init.*          → /project-init contract
    project-update.*        → /project-update contract
    cleanup-system.*        → Cleanup chain contract
    personality-system.*    → Personality framework
    research-safety.*       → Safety guardrails
    personalities/          → Personality definitions
    templates/              → Memory-system templates
  NOW.md                    → Runtime task state
  INDEX.md                  → Durable-memory router (not session-start entry)
  CHANGELOG.md              → Completed work history created by /archive
  agents/                   → Spawned-agent task artifacts
  rules/                    → Normative rules (standards, security, workflow)
  system/                   → System facts (architecture, caveats, testing)
  config/                   → Agent settings (personality, future plugins)
  related-projects/         → Cross-project issue/fix handoff log
  domains/                  → Topic-specific knowledge + decisions
  plans/                    → Active implementation plans
  daily/                    → Session logs
  archive/                  → Archived daily logs + completed plans
```

## Install

1. Clone or copy the `AIDOCS` folder anywhere.
2. Double-click `build/scripts/install-agent-routing.cmd`.

The installer wires the AIDOCS source path into the user's global agent config and installs the global command pack. Projects then consume AIDOCS through global commands plus project-local `/.MEMORY/` state.

`build/` is a generated release/runtime root, not this repo's live working memory surface. If you want the separation to be stronger operationally, publishing `build/` from a dedicated repo or release branch is the right model.

## Development

After editing system files at root, sync to build:

```
scripts/sync-build.cmd
```

This copies the distributable manifest from root to `build/`.

## Commands

| Command | Description |
|---------|-------------|
| `/memstart` | Warm startup context from `/.MEMORY/.aidocs` core setup docs plus project memory entry files |
| `/project-init` | Initialize a project: git bootstrap, copy system files, create memory structure |
| `/project-update` | Sync system files to latest without full re-init |
| `/reingest` | Primary ingestion command: ask user for full/git/date scope, then refresh memory accordingly |
| `/archive` | Distill daily logs into canonical memory, update `/.MEMORY/CHANGELOG.md`, and archive completed plans |
| `/personality` | Pick an agent communication personality |
| `/clean` | Remove orphaned/temp files from project |
| `/uber-clean` | `/clean` + dead code removal inside files |
| `/refactor` | `/uber-clean` + duplicate logic consolidation |
| `/uber-refactor` | `/refactor` + structural analysis & file splitting |
