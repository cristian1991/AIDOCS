# Workflow

Last verified: YYYY-MM-DD

- STOP format: print a blank line, then `🛑 STOP` (no separator lines).
- Session entry order: read `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/NOW.md`, then `/.MEMORY/INDEX.md`.
- Active implementation plans live in `/.MEMORY/plans/`, are referenced from `NOW.md`, and move to `/.MEMORY/archive/plans/` when completed.
- Spawned-agent artifacts: write plans/investigations to `/.MEMORY/agents/YYYY-MM-DD-<topic>-plan.md` or `/.MEMORY/agents/YYYY-MM-DD-<topic>-investigation.md`; promote durable findings into other canonical files in `/.MEMORY/**`.
- Related-project collaboration: when a fix here is caused by another project, log issue+fix handoff in `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md`.
- Task lifecycle memory rule: write NOW at task start; only the editing agent closes task memory by writing daily outcome, updating `Recent`, and cleaning NOW active-task state after edits are applied.
- STOP execution safety: if a STOP condition appears during a multi-step script/sequence, halt immediately and issue STOP; do not run remaining steps.
- Multi-repo/monorepo memory isolation: read/write only the active project's `/.MEMORY/` unless user explicitly requests cross-project memory actions.
- `/archive` behavior: create/update `/.MEMORY/CHANGELOG.md`, merge completed items from `NOW.md` `Recent` plus selected daily logs, move processed daily logs to `/.MEMORY/archive/`, move completed plan files to `/.MEMORY/archive/plans/`, then reset `NOW.md` to template placeholders.
- Memory write efficiency: keep memory entries as token-lean as possible while preserving exact user intent and constraints.
