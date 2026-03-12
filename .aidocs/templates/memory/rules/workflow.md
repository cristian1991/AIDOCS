# Workflow

Last verified: YYYY-MM-DD

- STOP format: print a blank line, then `🛑 STOP` (no separator lines).
- Memory entry order: read `/.MEMORY/INDEX.md` first, then `/.MEMORY/NOW.md`.
- Spawned-agent artifacts: write plans/investigations to `/agents/YYYY-MM-DD-<topic>-plan.md` or `/agents/YYYY-MM-DD-<topic>-investigation.md`; promote durable findings into `/.MEMORY/**`.
- Related-project collaboration: when a fix here is caused by another project, log issue+fix handoff in `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md`.
- Task lifecycle memory rule: write NOW at task start; only the editing agent closes task memory by writing daily outcome and cleaning NOW active-task state after edits are applied.
- STOP execution safety: if a STOP condition appears during a multi-step script/sequence, halt immediately and issue STOP; do not run remaining steps.
- Multi-repo/monorepo memory isolation: read/write only the active project's `/.MEMORY/` unless user explicitly requests cross-project memory actions.
- `/archive` behavior: snapshot `NOW.md` into `DONE.md`, then reset `NOW.md` to template placeholders.
- Memory write efficiency: keep memory entries as token-lean as possible while preserving exact user intent and constraints.
