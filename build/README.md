# AIDOCS

A portable toolkit for AI coding agents. Gives your agents persistent memory, structured commands, and consistent behavior across projects.

Works with **Claude Code** and **OpenCode**. Drop it into any project and your agent gets superpowers.

## Install

### First time (global bootstrap)

1. Clone or copy the `AIDOCS` folder anywhere on your machine.
2. Double-click `build/scripts/install-agent-routing.cmd`.

That's it. The installer wires up global routing files so every new agent session knows where AIDOCS lives:

- `%USERPROFILE%\.claude\CLAUDE.md` — Claude Code global config
- `%USERPROFILE%\.config\opencode\AGENTS.md` — OpenCode global config
- Copies all commands to both agent profiles

### Initialize a project

Open your project in Claude Code or OpenCode and run:

```
/project-init
```

This will:
- Set up git (if needed)
- Copy AIDOCS system files into the project (self-contained, portable)
- Create the `/.MEMORY/` structure for persistent memory
- Generate `AGENTS.md` and `CLAUDE.md` routers

Then run:

```
/memory-update
```

This reads your project's documentation and ingests it into structured memory categories the agent can reference later.

### Update an existing project

When AIDOCS gets new commands or updated docs:

```
/project-update
```

Syncs system files without re-running full initialization. Your memory and project-specific config are preserved.

## Commands

### Project Management

| Command | What it does |
|---------|-------------|
| `/project-init` | Initialize a new project: git bootstrap, copy system files, create memory structure |
| `/project-update` | Sync system files to latest version without full re-init |
| `/memory-update` | Read project docs and ingest into structured `/.MEMORY/` categories |
| `/archive` | Distill daily session logs into canonical memory categories |

### Cleanup Chain

An escalating chain of cleanup commands. Each level includes everything from the previous level.

```
/clean → /uber-clean → /refactor → /uber-refactor
```

| Command | What it does |
|---------|-------------|
| `/clean` | Remove orphaned files — temp files, debug artifacts, completed plans, empty placeholders. File-level only, never touches code inside files. |
| `/uber-clean` | Everything `/clean` does + remove dead code inside files: unused functions, dead imports, commented-out code blocks, unused CSS selectors. |
| `/refactor` | Everything `/uber-clean` does + consolidate duplicate logic: merge near-identical functions, deduplicate DTOs/models, extract repeated inline patterns. |
| `/uber-refactor` | Everything `/refactor` does + structural analysis: identify oversized files, SRP violations, god-classes, mixed concerns. Proposes file splits with specific cut points. |

All cleanup commands **stop and ask for confirmation** before making changes. `/uber-refactor` asks for approval on **each structural change individually**.

### Fun Stuff

| Command | What it does |
|---------|-------------|
| `/personality` | Let the agent pick a communication personality for user-facing messages. Available: `space-pirate`, `cat`, `robot`, `unicorn`, `default`. |
| `/personality default` | Kill switch — strips personality immediately, no questions asked. |
| `/personality <name>` | Load a specific personality by name. |

Personality only affects conversational tone — never code, commits, tool calls, or technical accuracy.

## How It Works

### Memory System (`/.MEMORY/`)

Each project gets a structured memory directory:

```
/.MEMORY/
  NOW.md              # Current task state (survives context compaction)
  INDEX.md             # Memory index
  rules/               # Normative rules (standards, security, workflow)
  system/              # System facts (architecture, caveats, testing)
  config/              # Agent settings (personality, future plugins)
  domains/             # Topic-specific knowledge + decisions
  plans/               # Implementation plans
  daily/               # Session logs (date-stamped)
  archive/             # Archived daily logs
```

- **NOW.md** is the agent's working memory — it writes current task state here before starting work and updates it as it goes. This is how agents survive context compaction and session restarts.
- **Daily logs** capture session-by-session progress. `/archive` promotes them into canonical categories.
- **Rules files** store durable user preferences and coding standards that persist across sessions.

### Documentation System (`.aidocs/`)

```
.aidocs/
  index.aidocs             # Documentation router (agent entry point)
  global-instructions.*    # Agent behavior rules
  coding-standards.*       # Code quality rules
  memory-system.*          # Memory protocol
  project-init.*           # /project-init contract
  project-update.*         # /project-update contract
  memory-update.*          # /memory-update contract
  cleanup-system.*         # Cleanup chain contract
  personality-system.*     # Personality framework
  research-safety.*        # Safety guardrails
  personalities/           # Personality definitions
  templates/memory/        # Memory structure templates
```

### Agent Compatibility

AIDOCS supports both major AI coding agents:

| Agent | Commands location | Global config |
|-------|------------------|---------------|
| Claude Code | `.claude/commands/*.md` | `%USERPROFILE%\.claude\` |
| OpenCode | `.opencode/command/*.md` | `%USERPROFILE%\.config\opencode\` |

Every command exists in both formats. OpenCode commands include `agent: build` in their frontmatter.

## Project Structure

```
AIDOCS/
  .aidocs/                     # Documentation & contracts
  .claude/commands/            # Claude Code command files
  .opencode/command/           # OpenCode command files
  scripts/
    install-agent-routing.*    # Global installer
    sync-build.*               # Sync root -> build/
  build/                       # Distributable kit (synced from root)
  /.MEMORY/                    # Project memory (AIDOCS eats its own dogfood)
  AGENTS.md                    # OpenCode entry point
  CLAUDE.md                    # Claude Code entry point
  AIDOCS.md                    # Internal documentation
  README.md                    # This file (human-facing)
```

## Development

Edit system files at root, then sync to `build/`:

```
scripts/sync-build.cmd
```

The `build/` directory is the distributable artifact. The installer runs from `build/scripts/`.

## Requirements

- Windows (installer is `.cmd` + PowerShell)
- [Claude Code](https://claude.ai/code) and/or [OpenCode](https://github.com/opencode-ai/opencode)
- Git (for `/project-init` initialization)
- GitHub CLI (`gh`) recommended (for automatic repo creation)
