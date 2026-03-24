# AIDOCS Install

This install path targets the `1.0.0` release line. Rerun the installer after local AIDOCS updates to refresh the global command pack, OpenCode plugin, and Claude hook wiring.

Current installer status: Windows and Linux/macOS installer paths are shipped.

## Quick install

From this `core/` directory, run:

```powershell
scripts\install-agent-routing.cmd
```

## What the installer does

- writes global bootstrap routing files for OpenCode and Claude
- installs the global command packs
- points the global bootstrap to this directory as the AIDOCS source
- installs the OpenCode global plugin at `~/.config/opencode/plugins/aidocs.js`
- installs Claude Code user-level hooks for `UserPromptSubmit` and `PreToolUse`
- refreshes existing global OpenCode and Claude command files to the current release version
- creates `action_tokens/opencode/` links or copies for user-visible OpenCode language-pack access

## After install

In a target project:

1. run `/aidocs` to bootstrap or resume AIDOCS for the project
2. OpenCode will auto-load the AIDOCS plugin and gate core tool usage until managed mode is active
3. after `/aidocs`, normal Claude Code prompts are routed through the AIDOCS managed-mode hook path
4. after `/aidocs`, OpenCode gets AIDOCS system context plus compiled workflow-action summaries through the plugin path
5. rerun `build\scripts\install-agent-routing.cmd` when you want to refresh the global routing, OpenCode plugin, and Claude hook wiring

## Expected project routing

Initialized projects should route in this order:

1. `/.MEMORY/.aidocs/index.aidocs`
2. `/.MEMORY/INDEX.md`
3. `/.MEMORY/sessions/<session-id>/SESSION.md`

## Optional: MCP Server

For enhanced runtime enforcement and code/schema indexing:

```bash
cd mcp
pip install -e .
```

Then configure your agent's MCP server settings to run `aidocs-mcp` or `python -m aidocs_mcp.mcp_server`.

See `mcp/README.md` for details and `mcp/HOST_INTEGRATION.md` for agent integration contracts.

## Notes

- This directory is the canonical AIDOCS tree.
- Keep project-specific runtime memory inside each target project's own `/.MEMORY/`.
- OpenCode plugins are loaded automatically from `~/.config/opencode/plugins/` and `.opencode/plugins/`.
- OpenCode currently uses mirrored `action_tokens` for advisory prompt classification, not full runtime route execution.
- The installer creates `mcp/server/aidocs_mcp/action_tokens/opencode/` with links or fallback copies to the language YAML files so they are easy to inspect.
- Claude Code hooks receive JSON on stdin and are installed into `~/.claude/settings.json`.
- If you update AIDOCS itself, edit this tree first.
