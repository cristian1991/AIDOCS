# AIDOCS Dashboard

The dashboard is a Tauri desktop app (Rust backend + React/TypeScript frontend) for monitoring and controlling AIDOCS without using the terminal.

## What it shows

- **Projects** — every AIDOCS-initialized project on this machine, with health status + last activity
- **Sessions** — active and recent sessions per project, with current scope, owned files, and journal preview
- **Lanes** — live lane activity when the conductor is running multi-agent: which agent is in which lane, current task, last message
- **Conductor chat** — talk to the long-lived conductor directly; ask it to dispatch a task, pause a lane, route a follow-up
- **Token usage** — per-project / per-session / per-host counters
- **Skills + MCP registry** — installed skills, available MCP servers, install/uninstall from the UI
- **Settings** — scoped editor (global / project / session); the same store the CLI reads from
- **Setup wizard** — first-run flow that walks through host detection, plugin/hook install, project init

## What you can do from it

- Start, pause, resume, or stop a session
- Dispatch a task to a specific model (Claude / GPT / Gemini / local)
- Inject a message into a running lane ("focus on X" / "stop and ask first")
- Edit settings without opening a config file
- Browse and install MCP servers from a curated registry
- See exactly what an agent ran and which gate verdicts fired

## Install

The dashboard builds from `apps/aidocs-dashboard/`:

```bash
cd apps/aidocs-dashboard
npm install
npm run tauri build
```

Then launch via the platform shortcut helper:

```bash
core/scripts/launch-dashboard.cmd       # Windows
bash core/scripts/launch-dashboard.sh   # Linux/macOS
```

On Windows you can create a desktop shortcut:

```bash
powershell core/scripts/create-desktop-shortcut.ps1
```

Releases ship a signed installer for Windows (`AIDOCS-Setup.exe`). Linux and macOS dashboard binaries are on the roadmap.

## Architecture sketch

- **Frontend** — React + TypeScript + Vite. Lints via biome (the single authority — no ESLint/Prettier mix).
- **Backend** — Tauri / Rust. Cargo fmt + clippy + nextest enforced at PR-time on the self-hosted runner. Cargo deny + cargo audit run on every PR that touches the dashboard tree.
- **State** — talks to the same MCP server + SQLite stores as the CLI; the dashboard is not a separate runtime. Anything the dashboard can do, the CLI or MCP API can do (and vice versa).

## Settings scope

Settings are scoped to one of three levels:

| Scope | Stored where | Inherits from |
|-------|--------------|---------------|
| Global | User-level config (OS-appropriate) | (none) |
| Project | `/.MEMORY/config/settings.sqlite` | Global |
| Session | Session SQLite | Project → Global |

A setting unset at the session level falls back to project, then global. The dashboard surfaces this inheritance directly so you can see the effective value and where it came from.

## When to use the dashboard vs the CLI

- **Dashboard**: monitoring, conductor chat, settings, skills/MCP install, first-run setup, lane control during multi-agent work
- **CLI** (`aidocs setup`, `doctor`, `init`, `status`, `sync`, `benchmark`, `version`): scripting, CI, headless servers, anything you'd put in a Makefile

The dashboard is convenience, not a separate authority. If a setting is reachable from both, they edit the same store.
