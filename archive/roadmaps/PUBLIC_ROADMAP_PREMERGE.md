# Public Roadmap

This roadmap is for developers, contributors, and integrators who want to understand where AIDOCS is going and where community help is especially valuable.

It is intentionally simpler than the internal/versioned roadmap docs.

The goal is to answer:

- what AIDOCS is actively improving
- what kinds of contributions are useful
- where community help is especially wanted

## Current Priorities

### 1. Better collaboration continuity

We want AIDOCS to get much better at cross-session and cross-project work.

That means improving:

- structured handoffs
- stronger resume bundles
- current-state reconstruction
- cross-project session linking
- freshness and trust signals for old summaries

Community help wanted:

- ideas for handoff schemas
- resume-summary UX
- operator-facing collaboration views
- examples of multi-session pain points in real projects

### 2. Stronger benchmark culture

We want benchmarks to reflect real usage, not fake marketing numbers.

Benchmarking is important, but it is not the first release priority when core tools still need to become more production-grade for deep work.

That means:

- realistic vague prompts
- code + schema + workflow-heavy scenarios
- multilingual prompt sets
- indexed-vs-raw comparisons
- public benchmark contracts with private corpus support where needed

Community help wanted:

- realistic public benchmark prompt sets
- public-safe sample repos
- benchmark automation improvements
- benchmark review and critique

### 3. User-extensible indexing

This is one of the most important long-term directions.

AIDOCS should support:

- strong built-in indexing where it already understands a language well
- safe declarative extension for unsupported or partially supported languages
- future community upgrades for richer language support

Examples people care about:

- C / C++
- Dart / Flutter
- Swift
- Lua
- Elixir
- R
- custom DSLs and templates

Community help wanted:

- language descriptor ideas
- heuristic outline rules
- project examples that need new language coverage
- parser-backed extractor contributions where justified

## Near-Term Product Direction

### CLI and operator surfaces
We are making the `aidocs` CLI stronger and more automation-friendly.

Current focus:

- JSON output
- benchmark/export support
- cleaner config/runtime surfaces

Community help wanted:

- CLI ergonomics feedback
- operator workflows
- automation use cases

### Host integrations
Claude and OpenCode are both important, but they do not have identical capabilities.

Current focus:

- clearer host contracts
- better OpenCode behavior where the host allows it
- keeping the docs honest about differences

Community help wanted:

- host integration testing
- plugin ergonomics feedback
- MCP client integration examples

### Docs and product clarity
We want AIDOCS to feel like a coherent product, not just a pile of good ideas.

Current focus:

- cleaner docs
- stronger product boundaries
- future docs-site structure under `docs.codenexus.cloud/aidocs`

### Tool quality before benchmark marketing
The immediate priority is making the retrieval and workflow tools strong enough that benchmarks actually mean something.

Current focus:

- deeper workflow discovery
- more precise service/method/enum/constructor retrieval
- better tuning of breadth, depth, and noise in investigate/trace flows

Community help wanted:

- real-world tool-usage feedback
- hard retrieval cases from large projects
- examples where the current tools are still too shallow or noisy

- docs improvements
- examples/tutorials
- clearer installation and onboarding guidance

## Best Contribution Areas

If you want high-leverage contribution areas, start here:

1. language/indexing extensibility
2. benchmark scenarios and public benchmark review
3. host integration improvements
4. docs/operator experience
5. collaboration continuity and handoff design

## What We Are Not Trying To Fake

We do not want to claim:

- universal first-class support for every language
- fake benchmark superiority from toy prompts
- full shared-session behavior when the host still owns the true session model
- identical host behavior where the host capabilities are actually different

## Where To Look Next

- `README.md` — product overview
- `mcp/README.md` — runtime and tool model
- `mcp/BENCHMARKS.md` — benchmark contract
- `mcp/ROADMAP.md` — current product-hardening roadmap
- `mcp/ROADMAP_1_3_0.md` — collaboration continuity direction
- `mcp/INDEXING_2_0_0_SPEC.md` — user-extensible indexing direction

## Short Version

If you want to help AIDOCS most:

- help make indexing more extensible
- help make benchmarks more real
- help make cross-session work less lossy
- help make the product easier to understand and adopt
