# AIDOCS.md - Root Documentation Router

This is the root documentation router for all projects under Root.

Definition: Root is the directory that contains `AIDOCS.md`.
All agents must read this file before starting any task in any project.

## Agent Checklist
1) Locate Root by traversing upward until `AIDOCS.md` is found.
2) Stay in-scope: do not operate outside Root unless explicitly instructed.
3) Read required docs in order (next section).
4) If the current project has `AGENTS.md`, read it after the Required Reading Order and before making changes.
5) If the project lacks `AGENTS.md`, run `/project` to generate it.

## Scope
- Do not read, write, run commands, or reference anything outside Root unless explicitly instructed.

## Required Reading Order
1) `AIDOCS.md`
2) `.aidocs/index.aidocs`
3) Any project-specific docs linked from the index.

## Agent Routing Rules
- If a project has `AGENTS.md`, read it after completing the Required Reading Order and before making changes.
- If missing, create it using the `/project` command and follow its output.

## Router Policy
- Router files must list/link docs; avoid forced full loading/import of all docs.
- When task context is insufficient, read only necessary related docs and memory files; if still unclear, STOP and ask.

## Cross-Agent Compatibility
- `AGENTS.md` and `CLAUDE.md` in Root are compatibility entry points that route to this file and `.aidocs/index.aidocs`.
- Canonical source remains `AIDOCS.md` + `.aidocs/*`.
- Optional global bootstrap installer for local user profiles: `scripts/install-agent-routing.cmd`.

## Command Entry Point
Use the `/project` command to initialize a project session. It will:
- Read root docs and project docs.
- Scan the codebase and DB artifacts.
- Restructure internal documentation into categorized project memory.
- Create or refresh `AGENTS.md` in the project root.
