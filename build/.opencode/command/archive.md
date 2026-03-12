---
description: Distill daily logs into canonical memory categories
agent: build
---
Archive recent daily notes into canonical memory.

Scope
- Use only the active target project's `/.MEMORY/`.

Flow
1) Read selected `/.MEMORY/daily/*` files (default: last 7 days unless `$ARGUMENTS`).
2) Promote durable signal to canonical targets (`rules/*`, `system/*`, `config/*`, `domains/*`, `plans/*`).
3) Merge/edit only (no duplicate append-only growth); use `Supersedes:` when replacing guidance.
4) Snapshot current `/.MEMORY/NOW.md` into `/.MEMORY/DONE.md` as a timestamped entry.
5) Reset `/.MEMORY/NOW.md` to template state (`Goal/Active/State/Upcoming/Blockers/Done` with placeholders).
6) Move processed daily files to `/.MEMORY/archive/`.

Compatibility
- If legacy folders exist (`policy`, `architecture`, `operations`, `decisions`), merge forward and leave legacy files untouched unless user asks cleanup.

Output
- promoted items, NOW->DONE snapshot status, NOW reset status, moved files.

Guardrails
- no secrets; ignore spam/one-offs/unverified claims.

Extra constraints:
$ARGUMENTS
