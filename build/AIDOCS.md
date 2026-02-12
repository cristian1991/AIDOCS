# AIDOCS

A portable toolkit for AI coding agents. Provides persistent memory, structured commands, and consistent behavior across projects.

This file is internal project documentation — agents do not need to read it. Agent entry points are `AGENTS.md` and `CLAUDE.md`, which route to `.aidocs/index.aidocs`.

## Architecture

```
.aidocs/
  index.aidocs             → Documentation router (agent entry point)
  global-instructions.*    → Agent behavior rules
  coding-standards.*       → Code quality rules
  memory-system.*          → Memory protocol
  project-init.*           → /project-init contract
  project-update.*         → /project-update contract
  memory-update.*          → /memory-update contract
  cleanup-system.*         → Cleanup chain contract
  personality-system.*     → Personality framework
  research-safety.*        → Safety guardrails
  personalities/           → Personality definitions
  templates/memory/        → Memory structure templates

.claude/commands/          → Claude Code command files
.opencode/command/         → OpenCode command files
scripts/                   → Installer + build sync scripts

build/                     → Distributable kit (synced from root via scripts/sync-build)
  .aidocs/                 → Copy of system docs
  .claude/commands/        → Copy of Claude commands
  .opencode/command/       → Copy of OpenCode commands
  scripts/                 → Installer (runs from build/)
  AGENTS.md, CLAUDE.md     → Template routers
  opencode.jsonc           → OpenCode config

/.MEMORY/                  → Project memory (initialized for AIDOCS itself)
  NOW.md                   → Current task state
  INDEX.md                 → Memory router
  rules/                   → Normative rules (standards, security, workflow)
  system/                  → System facts (architecture, caveats, testing)
  config/                  → Agent settings (personality, future plugins)
  domains/                 → Topic-specific knowledge + decisions
  plans/                   → Implementation plans
  daily/                   → Session logs
  archive/                 → Archived daily logs
```

## Install

1. Clone or copy the `AIDOCS` folder anywhere.
2. Double-click `build/scripts/install-agent-routing.cmd`.

The installer wires the AIDOCS source path into the user's global agent config so `/project-init` and `/project-update` know where to copy files from.

## Development

After editing system files at root, sync to build:

```
scripts/sync-build.cmd
```

This copies the distributable manifest from root to `build/`.

## Commands

| Command | Description |
|---------|-------------|
| `/project-init` | Initialize a project: git bootstrap, copy system files, create memory structure |
| `/project-update` | Sync system files to latest without full re-init |
| `/memory-update` | Read project docs and ingest into `/.MEMORY/` categories |
| `/archive` | Distill daily logs into canonical memory categories |
| `/personality` | Pick an agent communication personality |
| `/clean` | Remove orphaned/temp files from project |
| `/uber-clean` | `/clean` + dead code removal inside files |
| `/refactor` | `/uber-clean` + duplicate logic consolidation |
| `/uber-refactor` | `/refactor` + structural analysis & file splitting |
