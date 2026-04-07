# Workflow Gaps

- Global install currently overwrites `~/.config/opencode/AGENTS.md` and `~/.claude/CLAUDE.md` directly. There is no fresh-vs-update mode and no backup path yet.
- Local project initialization does the opposite: existing project `AGENTS.md` / `CLAUDE.md` are skipped, not rewritten or backed up, so local setup is non-destructive but not deterministic.
- OpenCode MCP settings are created globally by the installer in `~/.config/opencode/opencode.json[c]`; Claude project `.mcp.json` is created later by runtime code (`ensure_claude_mcp_config(...)`), not by the installer.
- `AIDOCS_PATH` currently points to the chosen AIDOCS source tree at install time, not to a dedicated installed runtime location like `~/.aidocs`.
- The optional MCP runtime install is manual because the installer wires host config and paths but does not own a dedicated Python dependency environment.
- OpenCode normal prompt handling does not call `aidocs_route_prompt(...)` at hook time. It relies on plugin-local state resolution, local prompt classification, and injected context.
- Claude normal prompt handling does call runtime classification/routing (`host_state(...)` + `aidocs_route_prompt(...)`) before injecting prompt context.
- OpenCode can fall back to filesystem-derived startup state when runtime host-state spawning fails, so its startup branch decisions can degrade to local heuristics instead of runtime-owned state.
- Project initialization and bootstrap wiring do not activate managed mode by themselves. `/.MEMORY/config/aidocs-managed.json` is only written later through `ManagedModeService.set_mode(...)` in the successful `/aidocs` orchestration path.
