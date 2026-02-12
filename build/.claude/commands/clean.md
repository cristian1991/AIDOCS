---
description: Remove orphaned and one-time-use files from project
---
Run `/clean` to sweep the project of agent-generated clutter.

Rules
- Requires `/.MEMORY/` to exist (run `/project-init` first if missing).
- Work across the entire project (root + all subdirectories).

Flow
1) Validate project initialized (`/.MEMORY/` exists).
2) Scan the project for orphaned/one-time-use files:
   - Completed plan files in `/.MEMORY/plans/` (check NOW.md Done section for confirmation)
   - Tool-generated temp files (`.claude/plan.md`, random `.tmp`, `.bak` files)
   - Agent-generated debug/verification artifacts (e.g., `verify_*.html`, `debug_*.log`)
   - Empty placeholder files that were created but never filled in
   - Orphaned scratch files in wrong locations (plans outside `/.MEMORY/plans/`, etc.)
3) Present the list of files found to the user with a summary of each.
4) STOP and ask for confirmation before deleting anything.
5) Delete confirmed files.
6) Output concise cleanup report: deleted files, skipped files, bytes reclaimed.

Scope boundaries
- Files only — does NOT touch code inside files.
- Does NOT delete: source code, config, docs, memory files in active use, `.gitignore`d build artifacts.
- Does NOT delete files the user created manually (only agent-generated clutter).
- When uncertain if a file is orphaned, ask — don't delete.

Guardrails
- Always STOP before deleting. Never auto-delete without confirmation.
- Log cleanup actions to today's daily file.

Extra constraints:
$ARGUMENTS
