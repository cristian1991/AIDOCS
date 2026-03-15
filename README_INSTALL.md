# AIDOCS Install

## Quick install

From this `build/` directory, run:

```powershell
scripts\install-agent-routing.cmd
```

## What the installer does

- writes global bootstrap routing files for OpenCode and Claude
- installs the global command packs
- points the global bootstrap to this directory as the AIDOCS source

## After install

In a target project:

1. run `/project-init` to create the project-local AI system
2. use `/memstart` at session start or after resume/compaction
3. use `/project-update` when you refresh the installed toolkit

## Expected project routing

Initialized projects should route in this order:

1. `/.MEMORY/.aidocs/index.aidocs`
2. `/.MEMORY/NOW.md`
3. `/.MEMORY/INDEX.md`

## Notes

- This directory is the canonical AIDOCS tree.
- Keep project-specific runtime memory inside each target project's own `/.MEMORY/`.
- If you update AIDOCS itself, edit this tree first.
