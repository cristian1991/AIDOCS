# AIDOCS Install Guide

Current release and test counts are shown by the badges at the top of [`README.md`](README.md). They read from generated artifacts — no hand-written version here so this page can't go stale between deploys.

## Recommended: pip install

```bash
pip install aidocs-mcp
aidocs setup /path/to/your/project
aidocs doctor
```

After `aidocs setup` completes, open the project in your IDE and type `/aidocs` to bootstrap a session.

## Windows: Desktop Installer

Download `AIDOCS-Setup.exe` from [Releases](https://github.com/cristian1991/AIDOCS/releases). Double-click — setup wizard handles everything including Python installation.

## Linux/macOS: One-line install (no Python required)

```bash
curl -fsSL https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.sh | bash
aidocs setup /path/to/your/project
aidocs doctor
```

If `aidocs` is not found after install, restart your shell so the updated PATH takes effect.

## Developer install (from source)

```bash
cd mcp && pip install -e .
aidocs setup
aidocs doctor
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
5. rerun `aidocs setup` when you want to refresh hooks, MCP config, or OpenCode plugin

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

For OpenCode, AIDOCS now prefers `opencode.jsonc` when it already exists and otherwise falls back to `opencode.json`.

See `mcp/HOST_INTEGRATION.md` for agent integration contracts.

### Optional: NLP user-intent detection

Default install uses deterministic regex detection for user-intent grants
("allow psql", "forced work on", etc.). The optional `[nlp]` extra adds
dependency-parse + lemma-aware detection so paraphrases like "you may
use grep" or "go ahead and delete the cache" are caught correctly, and
non-English prompts are detected via `lingua-py` before being routed to
a language-specific spaCy model.

```bash
cd mcp
pip install -e ".[nlp]"
python -m spacy download en_core_web_sm
# Optional per-language downloads:
#   python -m spacy download de_core_news_sm
#   python -m spacy download es_core_news_sm
```

All NLP deps are permissive licensed (MIT or Apache-2.0). The backend
is off by default; enable via the dashboard toggle `intent.nlp_enabled`.

## Dashboard

The AIDOCS Dashboard is a Tauri desktop app for monitoring projects, sessions, token usage, and settings.

```bash
# Launch (uses built binary or falls back to dev mode)
core\scripts\launch-dashboard.cmd       # Windows
bash core/scripts/launch-dashboard.sh   # Linux/macOS

# Create desktop shortcut (Windows)
powershell core\scripts\create-desktop-shortcut.ps1
```

The dashboard requires the Tauri app to be built first:
```bash
cd apps/aidocs-dashboard
npm install
npm run tauri build
```

## Notes

- This directory is the canonical AIDOCS tree.
- Keep project-specific runtime memory inside each target project's own `/.MEMORY/`.
- OpenCode plugins are loaded automatically from `~/.config/opencode/plugins/` and `.opencode/plugins/`.
- Claude Code hooks receive JSON on stdin and are installed into `~/.claude/settings.json`.
- If you update AIDOCS itself, edit this tree first.
