---
description: Archive or remove a selected session explicitly
---
Delete-session is the only explicit session cleanup flow.

Flow
1) Resolve root (`$ARGUMENTS` or cwd).
2) Require `/.MEMORY/sessions/`, else STOP.
3) Inspect available sessions under `/.MEMORY/sessions/*/SESSION.md`.
4) Issue STOP and ask which session should be deleted/archived.
5) Issue STOP and ask which deletion mode to use:
   1. `archive-session` (Recommended) — move the whole session folder to `/.MEMORY/archive/sessions/`
   2. `delete-session` — remove the session folder after explicit confirmation
6) If archiving:
   - update the session status to `done` if it is not already
   - move the whole session folder to `/.MEMORY/archive/sessions/`
   - keep `plans/`, `agents/`, `artifacts/`, and `context.md` with the session folder
7) If deleting:
   - require an explicit confirmation after the session is selected
   - remove only the selected session folder
8) Preserve canonical memory and changelog entries; do not remove durable memory automatically.
9) Do not rewrite or remove durable links in `/.MEMORY/INDEX.md` as part of session deletion.
10) Report what was archived or deleted.

Guardrails
- Never delete a session without explicit user confirmation.
- Prefer archival over deletion.
- Do not rewrite durable canonical memory as part of session deletion.

Extra constraints:
$ARGUMENTS
