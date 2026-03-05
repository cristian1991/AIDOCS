---
description: Distill daily logs into canonical memory categories
---
Archive recent daily notes into canonical memory.

Flow
1) Read selected `/.MEMORY/daily/*` files (default: last 7 days unless `$ARGUMENTS`).
2) Promote durable signal to canonical targets (`rules/*`, `system/*`, `config/*`, `domains/*`, `plans/*`).
3) Merge/edit only (no duplicate append-only growth); use `Supersedes:` when replacing guidance.
4) Move processed daily files to `/.MEMORY/archive/`.

Compatibility
- If legacy folders exist (`policy`, `architecture`, `operations`, `decisions`), merge forward and leave legacy files untouched unless user asks cleanup.

Output
- promoted items, skipped noise, moved files.

Guardrails
- no secrets; ignore spam/one-offs/unverified claims.

Extra constraints:
$ARGUMENTS
