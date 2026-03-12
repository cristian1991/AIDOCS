# Memory System

Last verified: 2026-03-12

- Decision: `/.MEMORY/INDEX.md` is the main project memory entry point/router.
- Decision: `/.MEMORY/NOW.md` stores live task state and is reached via `INDEX.md`.
- Rule: Project routers must point to `/.MEMORY/INDEX.md`, then `/.MEMORY/NOW.md`, then `.aidocs/index.aidocs`.
- Decision: Claude requires stronger memory enforcement than OpenCode because Claude also auto-loads external `~/.claude/projects/.../memory/MEMORY.md` state.
- Rule: Claude auto-memory file must act as a strict redirect/bootstrap shim into project-local `/.MEMORY/`; it is not a durable memory store and must not preserve side content.
- Rule: Memory-routing wording must stay token-lean but close bypasses; avoid ambiguous language like "prefer", "usually", or bare directory references when a file entry point is required.
- Rule: Plan persistence must always point to `/.MEMORY/plans/<descriptive-name>.md`.
- Rule: Spawned-agent plans/investigations go to project-root `/agents/`, not `/.MEMORY/`.
- Rule: Spawned-agent filenames use dated logical names: `/agents/YYYY-MM-DD-<topic>-plan.md` or `/agents/YYYY-MM-DD-<topic>-investigation.md`.
- Rule: Durable findings from `/agents/` must be promoted into canonical memory files in `/.MEMORY/**` when they matter beyond the current task.
- Rule: `/project-update` may normalize routing-critical memory router text without overwriting project-specific facts.
