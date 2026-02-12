---
description: Remove orphaned files and dead code from project
agent: build
---
Run `/uber-clean` to deep-clean the project — file cleanup plus dead code removal.

Rules
- Requires `/.MEMORY/` to exist (run `/project-init` first if missing).
- Includes everything `/clean` does, plus code-level cleanup.

Flow
1) Run `/clean` flow first (steps 1-6).
2) Scan source files for dead code:
   - Unused functions, methods, and variables (no callers/references)
   - Dead imports/usings (imported but never referenced)
   - Commented-out code blocks (dead code, NOT explanatory comments)
   - Unused CSS classes/selectors (defined but never referenced in views/HTML)
3) Present findings grouped by file, with context for each item.
4) STOP and ask for confirmation before editing. User may approve all, approve per-file, or skip items.
5) Remove confirmed dead code.
6) Output concise report: files modified, items removed per file, items skipped.

Scope boundaries
- Files + dead code inside files.
- Does NOT merge, restructure, or refactor — only removes provably dead code.
- Does NOT remove code that is referenced dynamically (reflection, string-based lookups, convention-based routing).
- Does NOT remove code marked with `// TODO`, `// HACK`, or similar intent markers.
- When uncertain if code is dead, ask — don't remove.

Guardrails
- Always STOP before editing code. Never auto-remove without confirmation.
- Verify "unused" claims by searching for all references before flagging.
- Log cleanup actions to today's daily file.

Extra constraints:
$ARGUMENTS
