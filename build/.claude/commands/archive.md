---
description: Distill daily logs into canonical memory categories
---
Run `/archive` with strict merge semantics.

Scope
- Work only in project `/.MEMORY/`.

Process
1) Read selected files from `/.MEMORY/daily/` (default recent 7 days unless `$ARGUMENTS` says otherwise).
2) Promote durable signal to canonical files:
   - policy -> `/.MEMORY/policy/*`
   - architecture -> `/.MEMORY/architecture/*`
   - decisions -> `/.MEMORY/decisions/DECISIONS_LOG.md`
   - operations -> `/.MEMORY/operations/*`
   - domain-specific -> `/.MEMORY/domains/<topic>.md`
3) Merge/edit existing entries; no duplicate append-only content.
4) Use `Supersedes:` when replacing prior guidance.
5) Move processed logs to `/.MEMORY/daily/archive/`.

Output
- concise per-file promotion summary
- skipped noise summary
- moved file list

Guardrails
- never store secrets
- ignore spam, one-off noise, unverified claims

Extra constraints:
$ARGUMENTS
