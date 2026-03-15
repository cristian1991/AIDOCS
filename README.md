# AIDOCS

AIDOCS is a portable AI coding-agent toolkit with a routed memory system, global command pack, and consistent cross-project startup behavior.

Agents do not use this file as an entry point. Project entry files are `AGENTS.md` and `CLAUDE.md`, which route into `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/NOW.md`, and `/.MEMORY/INDEX.md`.

## Canonical layout

`build/` is the canonical AIDOCS runtime/public tree.

```text
build/
  .commands/               -> canonical shared command source
  .MEMORY/
    .aidocs/               -> canonical instruction + memory-system docs
      index.aidocs
      global-instructions.aidocs
      coding-standards.aidocs
      memory-system.aidocs
      research-safety.aidocs
      personality-system.aidocs
      personalities/
      templates/
  AGENTS.md                -> project router template
  CLAUDE.md                -> project router template + Claude bootstrap note
  scripts/
    install-agent-routing.*
    check-memory-drift.*
```

Root-level files in this repo are support mirrors/dev wrappers for working on AIDOCS itself:

```text
/.MEMORY/                  -> this repo's own working memory system
AGENTS.md                  -> local router for this repo
CLAUDE.md                  -> local Claude router for this repo
README.md                  -> project documentation
README_INSTALL.md          -> install notes
```

## Memory model

Inside initialized projects, the memory system lives in project-local `/.MEMORY/`.

- `/.MEMORY/.aidocs/index.aidocs` -> session-start router
- `/.MEMORY/NOW.md` -> runtime task state
- `/.MEMORY/INDEX.md` -> durable-memory router
- `/.MEMORY/CHANGELOG.md` -> completed work history

Core split:

- `index.aidocs` -> route
- `global-instructions.aidocs` -> agent behavior
- `coding-standards.aidocs` -> coding rules
- `memory-system.aidocs` -> memory mechanics
- command files -> command logic

Command logic does not live in `.aidocs`; it lives in the global command pack.

## Install

Run the installer from `build/`:

```powershell
build\scripts\install-agent-routing.cmd
```

The installer:

- writes global bootstrap routing files for OpenCode and Claude
- installs the global command pack from `build/.commands/`
- points global bootstrap to the AIDOCS runtime/public root

More detail: `README_INSTALL.md`

## Commands

The canonical command source is `build/.commands/`.

Installed commands:

| Command | Description |
|---------|-------------|
| `/memstart` | Load startup context from the project memory routers |
| `/project-init` | Initialize a project with routed `/.MEMORY/` scaffolding |
| `/project-update` | Refresh project-local AI system files to current contracts |
| `/reingest` | Refresh memory by user-selected scope |
| `/archive` | Promote completed work into `/.MEMORY/CHANGELOG.md` and archive logs/plans |
| `/personality` | Set or clear user-facing communication personality |
| `/clean` | Run cleanup by user-selected scope (`file-clean`, `dead-code-clean`, `dedupe-clean`, `structural-clean`) |

## Development notes

- Treat `build/` as the canonical AIDOCS tree when updating the shipped system.
- Keep command logic in `build/.commands/` only.
- Keep memory-system mechanics in `build/.MEMORY/.aidocs/` only.
- Keep root support copies aligned with canonical `build/` content when needed.

## License

AIDOCS is licensed under Apache 2.0. See `LICENSE` and `NOTICE`.
