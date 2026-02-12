---
description: Ingest project docs into memory categories
agent: build
---
Run `/memory-update` to read project documentation and populate `/.MEMORY/` with structured knowledge.

Rules
- Work only inside the target project unless explicitly instructed.

Flow
1) Resolve target dir (`$ARGUMENTS` or current dir) and project root.
2) Validate project already initialized (must contain `/.MEMORY/`).
   - if not initialized, STOP and instruct to run `/project-init`.
3) Discover project docs/router + relevant README/spec docs.
4) Read source docs in order: root shared docs -> project entry files -> project docs/router -> relevant README/spec docs.
5) Build topic map and create/update `/.MEMORY/domains/<topic>.md`.
6) Ingest/restructure docs into canonical memory categories (merge/edit; no duplicate append-only content).
7) Output concise ingestion report: read files, skipped files, reasons, and category mapping.

Guardrails
- Merge/edit existing memory entries; never duplicate content.
- Do not overwrite user-authored content in memory files without explicit instruction.
- Never store secrets or sensitive data.
- Ignore spam, one-off noise, unverified claims.

Runtime memory
- Read `/.MEMORY/NOW.md` at task start before planning/actions.
- After compaction/session resume, re-read `/.MEMORY/NOW.md` before continuing (hard gate).
- Append notable outcomes to `/.MEMORY/daily/YYYY-MM-DD.md`.

Extra constraints:
$ARGUMENTS
