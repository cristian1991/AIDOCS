# Memory and Indexing

AIDOCS persists state and accelerates code reading. This page is the public deep-dive on both.

## The `/.MEMORY/` tree

Every initialized project owns a `/.MEMORY/` directory at the project root. The layout:

```
/.MEMORY/
  .aidocs/index.aidocs           # session-start router
  INDEX.md                        # durable-memory router
  sessions/<session-id>/
    SESSION.md                    # session state + scope
    context.md                    # session-local context
    journal.md                    # rolling decision log (auto-evicts)
    plans/                        # implementation plans
    agents/                       # spawned agent artifacts
    handoffs/                     # resume bundles
  rules/                          # workflow, coding, communication rules
  domains/                        # domain knowledge (entities, APIs, glossaries)
  archive/                        # evicted journal entries, completed sessions
  config/                         # project-scoped config (related projects, host adapters)
```

Files are the source of truth. SQLite indexes derived from them are rebuildable; if you delete the index, the next `aidocs sync` regenerates it from the files. The reverse is not true — if you delete a file, the index reflects the deletion on the next sync.

## Session lifecycle

A session is the unit of resumable work. Sessions have:

- **State**: pending / in-progress / blocked / paused / done / archived
- **Scope**: declared goal + constraints + relevant files
- **Routing**: when an agent runs `/aidocs`, the bootstrap router picks the most relevant session or asks the operator to choose; if a single suitable session exists, it auto-binds.

`aidocs setup` creates the bootstrap router files. `/aidocs` reads them. The Claude Code and OpenCode hook paths route every prompt through the active session once managed mode is on.

## Memory types

The capture API saves to typed files keyed by topic:

| Type | When to save | What it changes |
|------|--------------|-----------------|
| `user` | Role, preferences, knowledge, responsibilities | Tailors future explanations to who you are |
| `feedback` | Corrections + confirmations of approach | Stops the same mistakes; preserves validated judgment calls |
| `project` | Initiatives, deadlines, incidents, who-is-doing-what | Informs scope decisions and suggestion framing |
| `reference` | External system pointers (issue trackers, dashboards, design docs) | Quick redirect when a topic comes up |

Captured memories are loaded into future conversations when their topic surfaces. A `MEMORY.md` index at the top of the memory tree keeps the list short and scannable.

## Indexed retrieval

The retrieval surface is the primary code-reading path inside an AIDOCS-managed project. Raw `Read`/`Grep`/`Glob` on indexed files is gated; the gate returns an actionable error naming the right indexed tool.

Tool families:

- **Discovery** — `ai_find` (symbol / reference / text / route / mutation / validation / policy lookups), `ai_investigate` (concept → ranked containers), `ai_trace` (calls / field-flow / model / component / api-to-ui), `ai_bundle` (file / subsystem / partial / preset bundles), `ai_text_search`, `schema_query`
- **Reads** — `ai_get_lines`, `ai_get_symbol_snippet`, `ai_bundle(mode="file")`. These require the file to have been surfaced by a discovery tool, or `known_exact_path=true` for a path the operator named verbatim
- **Edits** — `ai_replace` (anchor / string / symbol / lines), `ai_edit_lines`, `ai_batch_str_replace`, `ai_batch_edit`, `ai_create_file`, `ai_insert_lines`

If a discovery call returns empty, the right move is to widen the query (different mode, switch to `ai_text_search`, bundle the parent directory). Reaching for raw tools when discovery returns empty is the failure mode the gate exists to catch.

## Semantic search

Local embedding model (`all-MiniLM-L6-v2`) runs offline. No external API call — the embeddings live in the project's SQLite index. Hybrid search ranks symbol + text + semantic hits together.

Install the embedding model with `pip install sentence-transformers`; without it, the symbol + text paths still work but semantic ranking falls back to text similarity.

## Auto-eviction

`journal.md` rolls old entries into `archive/` when it exceeds a configurable line budget. The session SQLite mirror records what was archived so a future search can still surface it without loading the full file.

## Related-project search

If a project has `/.MEMORY/config/related-projects.md` listing peer repos, the search tools can scope across them — `related_project_code_search`, `related_project_compare_concept`, `related_project_subsystem_bundle`. Each peer repo must itself be AIDOCS-initialized for the cross-repo indexes to align.

## What lives outside `/.MEMORY/`

The AIDOCS source itself (this repository) hosts the global command pack, the bootstrap router templates, and the runtime. Project memory always lives under the project's own root — AIDOCS never writes to a different tree.

`aidocs init <path>` creates `/.MEMORY/` for a project. `aidocs setup` configures global routing (hook entries, plugin install, MCP server registration) without touching project memory. The two are deliberately separate so a setup refresh doesn't disturb in-flight work.
