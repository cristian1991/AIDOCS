# Workflow

Last verified: YYYY-MM-DD

- STOP format: print a blank line, then `🛑 STOP` (no separator lines).
- Session entry order: read `/.MEMORY/.aidocs/index.aidocs`, then `/.MEMORY/INDEX.md`, then inspect `/.MEMORY/sessions/*/SESSION.md`, then read the selected `SESSION.md`.
- Active implementation plans live in `/.MEMORY/sessions/<session-id>/plans/`, are referenced from the selected `SESSION.md`, and stay with the session until explicit session archiving/deletion.
- Spawned-agent artifacts: write plans/investigations to `/.MEMORY/sessions/<session-id>/agents/`; promote durable findings into canonical files in `/.MEMORY/**`.
- Related-project collaboration: when a fix here is caused by another project, log issue+fix handoff in `/.MEMORY/related-projects/FIXES_BY_OTHER_AGENTS.md`.
- Task lifecycle memory rule: select or create a session at task start; only the editing agent closes task memory by updating the session and writing daily outcome.
- STOP execution safety: if a STOP condition appears during a multi-step script/sequence, halt immediately and issue STOP; do not run remaining steps.
- Multi-repo/monorepo memory isolation: read/write only the active project's `/.MEMORY/` unless user explicitly requests cross-project memory actions.
- `/archive` behavior: create/update `/.MEMORY/CHANGELOG.md`, merge completed items from completed session files plus selected daily logs, and move processed daily logs to `/.MEMORY/archive/`.
- Session folders persist by default and are archived/deleted only via an explicit `/delete-session` flow.
- Memory write efficiency: keep memory entries as token-lean as possible while preserving exact user intent and constraints.

## Automation Rules

- Add project-specific enforceable workflow rules here using the format `After <trigger>, <action>.`
- Use one workflow rule per bullet so `/aidocs` can compile it deterministically into `/.MEMORY/config/workflow-actions.json`.
- Supported triggers: after each completed task/change, after push, after GitHub workflow success, after deploy success.
- Supported actions: commit, push, commit and push, check git status, check GitHub Actions/workflow status, check deploy/VPS status, run `<local command>`, ssh `<host>` `<remote command>`.
- Use backticks around commands and SSH host/remote command values, for example: `After each completed task, run `python tools/blink.py`` and `After deploy success, ssh `prod` `systemctl status app``.
- Unsupported automation rules stay as human guidance until the workflow compiler/runtime supports them.
-
