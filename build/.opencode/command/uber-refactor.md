---
description: Full cleanup, deduplication, and structural analysis
agent: build
---
Run `/uber-refactor` for the full treatment — cleanup, dead code, deduplication, plus structural analysis and file splitting proposals.

Rules
- Requires `/.MEMORY/` to exist (run `/project-init` first if missing).
- Includes everything `/refactor` does, plus structural analysis.

Flow
1) Run `/refactor` flow first (steps 1-6).
2) Analyze all source files for structural issues:
   - Excessive file size (identify files that are too large for their responsibility)
   - Single-responsibility violations (files handling multiple unrelated concerns)
   - God-classes (classes with too many methods/responsibilities)
   - Overly deep inheritance chains or tight coupling
   - Files mixing concerns (e.g., data access + business logic + presentation in one file)
3) For each structural issue found, propose a split:
   - Specific cut points (e.g., "move methods X, Y, Z to `FooService.Queries.cs`")
   - Target file names and locations
   - Impact on imports/references
   - Estimated complexity of the change
4) STOP and ask for approval on EACH proposed split individually. These are architectural changes.
5) Apply approved splits one at a time, verifying after each.
6) Output concise report: issues found, splits applied, splits deferred, files created/modified.

Scope boundaries
- Full scope: files + dead code + deduplication + structural changes.
- Proposes structural changes — does NOT auto-apply without per-item approval.
- Does NOT change public API contracts without explicit approval.
- Does NOT introduce new patterns/frameworks — splits within existing architecture.

Guardrails
- STOP before EACH structural change. These are high-impact and hard to reverse.
- Create backups of files before splitting (to `/.BACKUP/uber-refactor/`).
- Run any available tests after each split if the user requests it.
- Log all structural changes to today's daily file.

Extra constraints:
$ARGUMENTS
