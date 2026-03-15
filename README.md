# AIDOCS

This directory is the canonical AIDOCS toolkit tree.

Use this directory when you want to develop, install, or publish AIDOCS.

## What is included

- `AGENTS.md`, `CLAUDE.md` - external router templates
- `.MEMORY/.aidocs/` - reusable memory-system contracts, startup docs, and templates
- `.claude/commands/` - Claude command pack
- `.opencode/command/` - OpenCode command pack
- `scripts/` - installer and utility scripts

## Install

Run:

```powershell
scripts\install-agent-routing.cmd
```

Or see `README_INSTALL.md` for the install flow.

## Notes

- Edit this tree first; mirror outward only when needed.
- Target projects keep runtime task state inside their own `/.MEMORY/` trees.
- `.MEMORY/.aidocs/` in this tree is the reusable AIDOCS system layer that gets installed into projects.

## Main commands

- `/memstart`
- `/project-init`
- `/project-update`
- `/reingest`
- `/archive`
- `/personality`
- `/clean`
- `/uber-clean`
- `/refactor`
- `/uber-refactor`
