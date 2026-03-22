# CLAUDE.md - AIDOCS Project Entry Point

Routing:
1) `/.MEMORY/.aidocs/index.aidocs` (session-start entry)
2) `/.MEMORY/INDEX.md` (durable-memory router)
3) inspect `/.MEMORY/sessions/*/SESSION.md`
4) read the selected session's `SESSION.md`
5) Only task-relevant files linked from those routers

Claude Memory Bootstrapping:
- External Claude memory file: `~/.claude/projects/<resolved>/memory/MEMORY.md`
- This file must be redirect/bootstrap only into project-local `/.MEMORY/`.
- It is not a durable memory store.

<!-- AIDOCS-MANAGED-ABOVE: write project-specific instructions below this line -->
