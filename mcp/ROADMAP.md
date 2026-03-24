# MCP Roadmap

This roadmap defines the complete end-state for the optional AIDOCS MCP layer.

`build/` remains the canonical Markdown-first AIDOCS system.
`mcp/` is the optional runtime enforcement, indexing, and retrieval layer over that canonical file-backed system.

## Final Product Direction

The end goal is not "an MCP experiment".

The end goal is:
- a production-complete MCP-backed AIDOCS runtime
- using the same file-backed canonical source of truth as the Markdown-first system
- with precise memory + code retrieval
- deeply integrated into agent/runtime workflows
- battle-tested on real repos

## Non-negotiables

- No second canonical memory store.
- No opaque migration of user data.
- No forced MCP dependency for Markdown-first users.
- `build/` remains canonical for contracts/templates.
- Plans must be complete and must state clearly defined production-ready end goals.
- SQLite/index layers are derived only and must be rebuildable from files.

## 1. Production-Complete Core

### Goal
Make the MCP-backed AIDOCS runtime reliable enough for daily use on real projects without manual babysitting for normal cases.

### Required capabilities
- Session lifecycle is complete:
  - list
  - select
  - create
  - update
  - delete/archive
- Memory lifecycle is complete:
  - read
  - capture durable memory
  - archive patch-notes changelog updates
- Project operations are complete:
  - project-init
  - project-update
  - legacy-update
- Managed marker handling is complete and safe.
- Drift/update tooling supports:
  - `check`
  - `check-legacy`
  - `fix`
- Windows and Linux/macOS entrypoints are supported.

### End goal definition
- A new project can be initialized cleanly.
- A current-format project can be updated safely.
- A legacy project can be upgraded with explicit migration choices.
- Safe structural fixes are deterministic.
- User-owned content below managed markers is never lost.

## 2. Canonical Contract Alignment

### Goal
Keep the MCP runtime aligned with the canonical AIDOCS contracts in `build/.MEMORY/.aidocs/` and never let MCP become a separate competing system.

### Required capabilities
- MCP respects:
  - `index.aidocs` as startup route
  - `global-instructions.aidocs` as behavior contract
  - `coding-standards.aidocs` as coding rules
  - `memory-system.aidocs` as mechanics
- Commands in `build/.commands/` remain the single source of command task logic.
- MCP methods map to the same lifecycle without contradicting the docs.

### End goal definition
- AIDOCS Markdown contracts and MCP behavior say the same thing.
- There is no duplicated competing lifecycle logic.
- A project can use Markdown-only or MCP-backed AIDOCS without semantic drift.

## 3. Session-Based Runtime Model

### Goal
Make sessions the only active runtime state model and remove all shared-runtime collision patterns.

### Required capabilities
- No shared active `NOW.md` runtime model.
- Active runtime state lives in:
  - `/.MEMORY/sessions/<session-id>/SESSION.md`
  - `context.md`
  - `plans/`
  - `agents/`
  - `artifacts/`
- Session persistence is the default.
- `/delete-session` is the only explicit cleanup path.
- Multi-agent work does not overwrite shared runtime state.

### End goal definition
- Parallel agents can work safely in the same repo.
- Session selection is explicit.
- Session-local plans and artifacts never leak back into global active runtime state.
- Legacy `NOW.md` / root plans / root agents patterns are fully deprecated.

## 4. MCP Runtime Enforcement

### Goal
Turn the AIDOCS Markdown lifecycle into actual runtime behavior instead of optional advice.

### Required capabilities
- Enforced session selection before deeper task work.
- Structured memory writes through MCP.
- Structured project update/init flows through MCP-aware tooling.
- Controlled archive/session delete behavior.
- Clear rules for when user-linked files may be inspected before full session grounding.

### End goal definition
- The agent no longer "maybe follows" the lifecycle.
- The host/runtime can reliably require the important steps.
- The system stays flexible enough for direct user-linked file inspection when appropriate.

## 5. Derived Memory Index

### Goal
Provide fast search and retrieval over canonical memory without creating a second truth layer.

### Required capabilities
- SQLite memory index for:
  - session summaries
  - memory file manifest
  - checksums
  - titles
  - searchable content
  - internal memory links
- Fast search over memory files.
- Status and rebuild support.

### End goal definition
- Deleting the DB does not lose anything important.
- Rebuilding the DB reproduces the same searchable memory graph from files.
- Agents can query memory precisely instead of re-reading broad file sets.

## 6. Production-Ready Code Retrieval

### Goal
Make code retrieval precise enough that memory can guide the agent to the right code without brute-force repo reading.

### Required capabilities
- Code file manifest and summaries.
- Symbol search.
- File outlines.
- Exact symbol/snippet retrieval.
- Dependency edges.
- Session-guided code bundles.
- Preset bundles.

### Current language targets
- Python
- JavaScript
- TypeScript
- JSX
- TSX
- C#

### End goal definition
- The code index narrows context instead of bloating it.
- Agents can reach the right code unit quickly.
- Retrieval helps more than raw file-search/grep in real projects.

## 7. C# Depth (Partials, DTOs, Models, Enums)

### Goal
Handle complex C# repos with partial-heavy architecture and dense DTO/model layers.

### Required capabilities
- Partial-aware indexing.
- Partial-group retrieval.
- DTO/model/data-structure indexing.
- Enum + enum-member indexing.
- Exact snippet retrieval for types and members.

### End goal definition
- Repos like `DentalClinic-WebApp` become understandable through the index.
- Partial classes no longer feel like disconnected files.
- DTO/model/enum context can be retrieved without brute scanning many files.

## 8. Frontend Behavior + Styling Retrieval

### Goal
Make frontend indexing useful without turning the DB into noise.

### Required capabilities
- Index:
  - components
  - hooks
  - named functions
  - initializers/startup hooks
  - dependency edges
- For styling-sensitive work:
  - when non-Tailwind classes are present in a class string, the system must help surface CSS styling connected to that element
  - relevant stylesheets/selectors should be discoverable as part of retrieval

### End goal definition
- Frontend retrieval surfaces the real behavior/styling anchors.
- The index does not bloat with every tiny local variable or JSX node.
- Styling issues caused by base/custom classes become easier to trace.

## 9. Dependency-Aware Bundles

### Goal
Move from isolated file lookups to context bundles that reflect how code actually works together.

### Required capabilities
- Dependency-aware file bundles.
- Reverse dependency lookup.
- Partial-aware bundles.
- Session-guided bundles.
- Preset bundles:
  - `csharp-partial`
  - `js-initializer`
  - `data-structure`
  - `session`
  - `dependency`

### End goal definition
- Common code-retrieval tasks can be handled with one or two high-level bundle calls.
- Bundles surface the right neighboring context without degenerating into giant dumps.

## 10. AST-Perfect Language Support

### Goal
Replace regex-first indexing with parser-first indexing where it materially improves correctness.

### Required capabilities
- Python via `ast`
- JS/TS/JSX/TSX via tree-sitter or equivalent parser
- C# via the best practical parser path (ideally Roslyn-backed or equivalent quality)

### Must improve
- symbol boundaries
- member extraction
- imports/usings
- initializer detection
- snippet boundaries
- component detection
- DTO/model detection
- partial type handling

### End goal definition
- Regex heuristics are no longer the primary source of truth for code structure.
- False positives/false negatives are low enough on real repos to trust the retrieval layer.

## 11. Battle-Test On Real Repos

### Goal
Prove the system on real-world complexity, not toy examples.

### Required testbeds
- `DentalClinic-WebApp`
- `AutoDeployBase`
- `Musicity`
- at least one frontend-heavy project
- at least one legacy-migration project

### Must prove
- session model holds under pressure
- legacy migration works safely
- C# partial retrieval is actually useful
- JS initializer and frontend retrieval is actually useful
- bundle presets reduce noise
- updater/checker keeps projects aligned safely

### End goal definition
- The system saves time instead of costing attention on real repos.
- You trust it enough to use it without constantly second-guessing the retrieval layer.

## 12. Packaging And Adoption

### Goal
Keep AIDOCS approachable for Markdown-first users while making MCP a first-class enhancement.

### Required capabilities
- `build/` remains a usable standalone Markdown-first system.
- `mcp/` remains optional but production-ready.
- installation and update docs are clear.
- package/runtime entrypoints are stable.

### End goal definition
- cautious users can adopt the Markdown system only
- power users can adopt MCP
- both paths stay aligned to the same contracts

## Completion Criteria

AIDOCS MCP is complete enough when all of these are true:

- The canonical Markdown contract is stable.
- Session runtime is the only active runtime model.
- Updater/checker/fix flow is safe and reliable.
- MCP runtime enforces the important lifecycle steps.
- Derived memory/code indexes are rebuildable and trustworthy.
- Code retrieval is precise enough to reduce noise in real repos.
- Frontend and C# edge cases are handled usefully.
- Real projects prove the system under pressure.
- The system improves focus instead of becoming a hindrance.
