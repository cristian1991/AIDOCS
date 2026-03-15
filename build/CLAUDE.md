# CLAUDE.md - AIDOCS Project Entry Point

Routing:
1) `/.MEMORY/.aidocs/index.aidocs` (session-start entry)
2) `/.MEMORY/NOW.md` (runtime task state)
3) `/.MEMORY/INDEX.md` (durable-memory router)
4) Only task-relevant files linked from those routers

Claude Memory Bootstrapping:
- External Claude memory file: `~/.claude/projects/<resolved>/memory/MEMORY.md`
- This file must be redirect/bootstrap only into project-local `/.MEMORY/`.
- It is not a durable memory store.
