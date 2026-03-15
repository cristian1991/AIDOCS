# Memory System

Last verified: 2026-03-15

- Decision: `/.MEMORY/.aidocs/index.aidocs` is the only session-start entry point.
- Decision: `/.MEMORY/NOW.md` stores live task state and is read before `/.MEMORY/INDEX.md`.
- Decision: `/.MEMORY/INDEX.md` is the durable-memory router, not the session-start entry.
- Decision: `/.MEMORY/CHANGELOG.md` is the completion-history surface created/updated by `/archive`.
- Rule: Project routers must point to `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`.
- Decision: Claude requires stronger memory enforcement than OpenCode because Claude also auto-loads external `~/.claude/projects/.../memory/MEMORY.md` state.
- Rule: Claude auto-memory file must act as a strict redirect/bootstrap shim into project-local `/.MEMORY/`; it is not a durable memory store and must not preserve side content.
- Rule: Memory-routing wording must stay token-lean but close bypasses; avoid ambiguous language like "prefer", "usually", or bare directory references when a file entry point is required.
- Rule: Active implementation plans live in `/.MEMORY/plans/<descriptive-name>.md` and must be referenced from `NOW.md` while in progress.
- Rule: Completed plan files move from `/.MEMORY/plans/` to `/.MEMORY/archive/plans/`.
- Rule: Spawned-agent plans/investigations go to `/.MEMORY/agents/`, not outside `/.MEMORY/`.
- Rule: Spawned-agent filenames use dated logical names: `/.MEMORY/agents/YYYY-MM-DD-<topic>-plan.md` or `/.MEMORY/agents/YYYY-MM-DD-<topic>-investigation.md`.
- Rule: Durable findings from `/.MEMORY/agents/` must be promoted into canonical memory files elsewhere in `/.MEMORY/**` when they matter beyond the current task.
- Rule: `TODO.md` and `DONE.md` are not part of the memory system and should not be created or retained.
- Rule: `/project-update` may normalize routing-critical memory router text without overwriting project-specific facts.
- Rule: `build/` is the runtime/public AIDOCS root for project setup and updates.
- Rule: `/project-update` must explicitly recopy core shared setup files into projects from `build/`: `/.MEMORY/.aidocs/index.aidocs`, `/.MEMORY/.aidocs/global-instructions.aidocs`, `/.MEMORY/.aidocs/coding-standards.aidocs`, and `/.MEMORY/.aidocs/memory-system.aidocs`.
- Rule: `/memstart` is the explicit startup/restart warm-up command; it must read `/.MEMORY/.aidocs/index.aidocs`, then core shared `/.MEMORY/.aidocs` setup files, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`, followed only by task-relevant linked files.
- Rule: On a new task, agents rewrite `NOW.md`, clear stale previous-task items from `Recent`, read memory/code context as needed, execute, write the completed result into `NOW.md` `Recent`, then report; STOP immediately if anything is unclear.
- Rule: `/archive` must merge completed items from `NOW.md` `Recent` and selected daily logs into `/.MEMORY/CHANGELOG.md`, then archive processed daily logs and completed plan files.
