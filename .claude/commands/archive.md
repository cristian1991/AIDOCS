---
description: Update changelog and archive completed work
---
Archive recent daily notes and completed work into canonical memory and `/.MEMORY/CHANGELOG.md`.

Scope
- Use only the active target project's `/.MEMORY/`.

Flow
1) Read current `/.MEMORY/NOW.md` plus selected `/.MEMORY/daily/*` files (default: last 7 days unless `$ARGUMENTS`).
2) Promote durable signal to canonical targets (`rules/*`, `system/*`, `config/*`, `domains/*`).
3) Create/update `/.MEMORY/CHANGELOG.md` from `NOW.md` `Recent` items plus completed items found in the selected daily logs.
4) Merge/edit only (no duplicate append-only growth); use `Supersedes:` when replacing guidance.
5) Move completed plan files from `/.MEMORY/plans/` to `/.MEMORY/archive/plans/`.
6) Reset `/.MEMORY/NOW.md` to template state (`Goal/Active/Plans/State/Upcoming/Blockers/Recent` with placeholders).
7) Move processed daily files to `/.MEMORY/archive/`.

Compatibility
- If legacy folders exist (`policy`, `architecture`, `operations`, `decisions`), merge forward and leave legacy files untouched unless user asks cleanup.

Output
- promoted items, changelog status, NOW reset status, moved daily files, moved plan files.

Guardrails
- no secrets; ignore spam/one-offs/unverified claims.

Extra constraints:
$ARGUMENTS
