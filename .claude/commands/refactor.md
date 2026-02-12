---
description: Clean dead code and consolidate duplicates
---
Run `/refactor` to deep-clean and deduplicate — file cleanup, dead code removal, plus merging duplicate logic.

Rules
- Requires `/.MEMORY/` to exist (run `/project-init` first if missing).
- Includes everything `/uber-clean` does, plus deduplication.

Flow
1) Run `/uber-clean` flow first (steps 1-6).
2) Scan for duplicate/near-duplicate logic:
   - Functions/methods with identical or near-identical implementations
   - Repeated LINQ queries or SQL patterns that could be shared helpers
   - Duplicate DTOs/models representing the same data shape
   - Repeated inline logic blocks (3+ occurrences of similar code)
   - Duplicate CSS utility patterns or repeated style blocks
3) For each group of duplicates, propose a consolidation:
   - Which implementation to keep (or new shared helper to create)
   - Where to place the shared version
   - Which call sites to update
4) STOP and ask for confirmation before refactoring. User may approve all, approve per-group, or skip items.
5) Apply confirmed consolidations.
6) Output concise report: duplicates found, merges applied, merges skipped, files modified.

Scope boundaries
- Files + dead code + deduplication within current file structure.
- Does NOT split files or change architecture — only consolidates within the existing structure.
- Does NOT rename public APIs without user approval.
- Prefers extracting to existing utility/helper files over creating new ones.

Guardrails
- Always STOP before refactoring. Never auto-merge without confirmation.
- Ensure merged functions preserve all edge cases from the originals.
- Run any available tests after refactoring if the user requests it.
- Log refactoring actions to today's daily file.

Extra constraints:
$ARGUMENTS
