---
description: Update changelog and archive completed work
---
Archive recent daily notes and completed work into canonical memory and `/.MEMORY/CHANGELOG.md`.

Scope
- Use only the active target project's `/.MEMORY/`.

Flow
1) Read selected session files whose status is `done` under `/.MEMORY/sessions/*/SESSION.md` plus selected `/.MEMORY/daily/*` files (default: last 7 days unless `$ARGUMENTS`).
2) Promote durable signal to canonical targets (`rules/*`, `system/*`, `config/*`, `domains/*`).
3) Create/update `/.MEMORY/CHANGELOG.md` from completed session summaries plus completed items found in the selected daily logs.
4) Merge/edit only (no duplicate append-only growth); use `Supersedes:` when replacing guidance.
5) Move processed daily files to `/.MEMORY/archive/`.

Session policy
- Do not archive or delete session folders during `/archive`.
- Sessions remain in `/.MEMORY/sessions/` until an explicit `/delete-session` flow is used.
- Session-local plans remain with their session; `/archive` does not move them.

Compatibility
- If legacy folders exist (`policy`, `architecture`, `operations`, `decisions`), merge forward and leave legacy files untouched unless user asks cleanup.

Output
- promoted items, changelog status, moved daily files.

Guardrails
- no secrets; ignore spam/one-offs/unverified claims.

Extra constraints:
$ARGUMENTS
