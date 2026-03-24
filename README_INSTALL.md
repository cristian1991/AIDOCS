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
- installs the OpenCode global plugin at `~/.config/opencode/plugins/aidocs.js`
- installs Claude Code user-level hooks for `UserPromptSubmit` and `PreToolUse`

## After install

In a target project:

1. run `/aidocs` to bootstrap or resume AIDOCS for the project
2. OpenCode will auto-load the AIDOCS plugin and gate core tool usage until managed mode is active
2. after `/aidocs`, normal Claude Code prompts are routed through the AIDOCS managed-mode hook path
3. after `/aidocs`, OpenCode gets AIDOCS system context plus compiled workflow-action summaries through the plugin path
4. rerun `build\scripts\install-agent-routing.cmd` when you want to refresh the global routing, OpenCode plugin, and Claude hook wiring

## Expected project routing

Initialized projects should route in this order:

1. `/.MEMORY/.aidocs/index.aidocs`
2. `/.MEMORY/INDEX.md`
3. `/.MEMORY/sessions/<session-id>/SESSION.md`

## Notes

- This directory is the canonical AIDOCS tree.
- Keep project-specific runtime memory inside each target project's own `/.MEMORY/`.
- OpenCode plugins are loaded automatically from `~/.config/opencode/plugins/` and `.opencode/plugins/`.
- Claude Code hooks receive JSON on stdin and are installed into `~/.claude/settings.json`.
- If you update AIDOCS itself, edit this tree first.
