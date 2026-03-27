# User-Extensible Indexing (v2.0.0 Direction)

## Theme

Move AIDOCS toward **user-extensible indexing**.

This does **not** mean claiming full first-class semantic support for every language or file type.

It means building an indexing architecture where:

- AIDOCS handles the languages and structures it knows well
- users can extend indexing behavior safely and declaratively
- community and open-source contributions can deepen language support over time

## Why This Matters

Real projects do not fit a small hardcoded language list.

Teams want to index:

- C / C++
- Dart / Flutter
- Swift
- Lua
- Elixir
- R
- custom DSLs
- domain templates
- infrastructure/config formats

AIDOCS should not pretend it can deliver equally rich semantic support for all of them immediately.

But it should provide a path where those projects can still benefit from indexing, retrieval, and future language-specific upgrades.

## Product Positioning

The right product claim is:

> AIDOCS provides strong built-in indexing for supported languages and a user-extensible indexing model for everything else.

Not:

> AIDOCS fully supports any language.

## Goals

### End Goal 1: Safe Declarative Extension
- Users can add indexable file types without patching AIDOCS source.
- Users can tune discovery and indexing behavior per project.

### End Goal 2: Layered Support Levels
- Not every language needs parser-level support on day one.
- AIDOCS should support multiple levels of indexing richness.

### End Goal 3: Community Upgrade Path
- Open-source contributors can improve a language from weak support to richer support over time.
- The architecture should encourage this instead of forcing all support into the core team.

### End Goal 4: Honest Capability Boundaries
- The system should say what level of support a language has.
- Users should not assume “indexed” means “fully understood.”

## Support Tiers

### Tier 0: Discovery Only
- files are discoverable
- files can be included in bundles/search space
- no semantic extraction promised

### Tier 1: Summary Indexing
- files get lightweight summaries
- file role hints may exist
- retrieval can reference them sensibly

### Tier 2: Heuristic Structure
- heuristic symbols/outlines via regex/pattern rules
- basic role inference
- no AST guarantee

### Tier 3: Built-In Rich Support
- parser-backed or language-aware extraction
- stronger symbols/outlines
- better dependency and role inference

### Tier 4: Advanced Ecosystem Support
- deeper language-specific features contributed over time
- optional plugins/adapters/parsers where justified

## What Should Be Extensible in v2.0.0

### 1. File Discovery
- custom extensions
- custom include globs
- custom exclude globs
- custom large-file thresholds

### 2. Module and Role Hints
- extra module-hint directories
- role hints by path pattern
- role hints by filename suffix/prefix

### 3. Heuristic Structure Rules
- regex/pattern-based symbol extraction
- container naming rules
- file-level summary hints

### 4. Language Registration Metadata
- name
- extensions
- support tier
- optional parser family or heuristic rule set

## What Should Not Be Fully Open-Ended at First

### 1. Arbitrary Runtime Code Plugins from Config
- too risky
- too hard to keep deterministic and safe

### 2. Unrestricted Parser Loading
- versioning and stability problems
- security and reproducibility concerns

### 3. Claims of Full Semantic Support via Config Alone
- misleading
- config should extend indexing, not fake deep understanding

## Proposed Config Direction

Possible future `aidocs.toml` shape:

```toml
[index]
extra_skip_dirs = "generated, snapshots"
extra_module_hints = "clients, packages"

[[index.language]]
name = "dart"
extensions = ".dart"
tier = "summary"
include_globs = "lib/**/*.dart,test/**/*.dart"
role_hint = "application"

[[index.language]]
name = "cpp"
extensions = ".c,.cc,.cpp,.h,.hpp"
tier = "heuristic"
include_globs = "src/**/*,include/**/*"
outline_rule_set = "c_like_basic"

[[index.role_pattern]]
glob = "**/*Controller.dart"
role = "request-surface"

[[index.role_pattern]]
glob = "**/*Repository.cpp"
role = "data-access"
```

## Proposed Internal Architecture

### Language Descriptor Registry
- built-in descriptors for first-class languages
- project-local descriptors loaded from config
- optional future shared/community descriptor packs

### Rule Engine
- file-discovery rules
- path-based role rules
- summary rules
- heuristic outline extraction rules

### Capability Metadata
- each indexed language/file type should record:
  - support tier
  - source of support (`built_in`, `project_config`, `community_pack`)
  - limitations if relevant

## Contribution Model

### Core team owns
- indexing architecture
- safety model
- built-in first-class languages
- support-tier semantics

### Community can contribute
- language descriptors
- heuristic rule sets
- richer built-in language upgrades
- parser-backed extractors where justified
- docs/examples for project-specific language config

## Release Phasing Suggestion

### v2.0.0
- declarative language registration
- include/exclude patterns
- module/role hints
- support-tier reporting
- heuristic rule sets for a few expandable families

### v2.1.x+
- curated community language packs
- better per-language validation
- richer extractors for high-demand languages like Dart/Flutter or C/C++

### later
- optional plugin/adaptor ecosystem if the safety story is strong enough

## Risks

- users may overestimate weak-support languages
- heuristic indexing can create noisy or misleading outlines
- too much flexibility can make support/debugging harder

## Risk Controls

- always expose support tier
- keep built-in vs custom support visible
- add validation for config-defined languages
- keep unsafe dynamic plugin loading out of the first version

## Success Criteria

User-extensible indexing is successful when:

- teams can add meaningful indexing support for unsupported languages without patching core code
- indexed retrieval becomes useful on more real-world repos
- the system stays honest about support quality
- community language upgrades can improve support over time without redesigning the architecture

## Summary

`v2.0.0` should make AIDOCS extensible, not magical.

The right direction is:

- strong built-in support where AIDOCS is confident
- safe declarative extension where users need flexibility
- community-driven upgrades where the ecosystem has expertise

That is the scalable path toward broader language coverage without pretending every file type can be abstracted into equal semantic quality on day one.
