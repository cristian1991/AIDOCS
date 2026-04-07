# Index Language Descriptors

This document describes the current TOML-based language descriptor system used by AIDOCS indexing.

## Purpose

Language descriptors let AIDOCS know:

- which files belong to a language
- what support tier that language has
- what heuristic semantics may apply
- what role/module hints should influence indexing

Built-in descriptors ship with AIDOCS.

Project-local descriptors can be added in:

```text
<project-root>/index_languages/*.toml
```

## Built-In vs Project-Local

- built-in descriptors live in:
  - `mcp/server/aidocs_mcp/index_languages/`
- project-local descriptors live in:
  - `<project-root>/index_languages/`

Project-local descriptors can extend or override built-in behavior.

## Current Supported Keys

### Discovery keys
A descriptor should declare at least one of:

- `extensions`
- `suffixes`
- `include_globs`

### Core keys

- `name`
- `extensions`
- `suffixes`
- `include_globs`
- `tier`
- `source` (reported by the runtime/index, not usually authored directly)

### Semantics keys

- `semantic_tags`
- `outline_family`
- `outline_patterns`
- `role_hint`
- `role_patterns`
- `module_hints`

These semantics now influence real indexing behavior, not just metadata.

## Minimal Example

```toml
name = "dart"
extensions = [".dart"]
tier = "heuristic"
```

## Heuristic Semantics Example

```toml
name = "r"
extensions = [".r", ".R"]
tier = "heuristic"
role_hint = "analysis-script"
module_hints = ["analyses"]

outline_patterns = [
  { pattern = '^\\s*([A-Za-z_][A-Za-z0-9_.]*)\\s*<-\\s*function\\s*\\(', kind = "function" },
]
```

## Semantic Tags

For common built-in language/application shapes, descriptors can use simple tags instead of long repetitive role/module blocks.

Example:

```toml
name = "typescript"
extensions = [".ts"]
semantic_tags = ["typescript_app"]
```

Current built-in tags can be inspected via MCP using:

- `index_language_descriptor_semantics_get`

These tags expand internally into descriptor semantics such as role patterns and module hints.

## Outline Families

For common heuristic families, descriptors can use:

```toml
outline_family = "rust_basic"
```

Current built-in families can be inspected via MCP using:

- `index_language_descriptor_semantics_get`

Current built-in families:

- `rust_basic`
- `go_basic`
- `java_basic`
- `kotlin_basic`
- `ruby_basic`
- `php_basic`
- `elixir_basic`
- `frontend_script_basic`
- `sql_ddl_basic`

## Validation

Descriptors can be validated via MCP using:

- `index_language_descriptors_validate`

Validation currently checks for:

- parse errors
- missing discovery keys
- extension collisions
- suffix collisions
- unknown outline-family names

Descriptors can also be inspected from the CLI:

- `aidocs descriptors`
- `aidocs descriptors --validate`
- `aidocs descriptors --match <path>`

## Current Scope

Implemented now:

- TOML-backed built-in descriptors
- project-local descriptor overrides/extensions
- cached merged registry
- support tier/source metadata
- heuristic outline patterns
- built-in outline families
- role hints and role patterns
- module hints
- descriptor inspection from MCP and CLI
- descriptor match inspection for project-relative paths
- descriptor metadata surfaced in index status and retrieval results

Still future work:

- richer support-tier-aware behavior in retrieval/ranking
- more semantic shorthand/tag families
- community descriptor packs
- more advanced declarative semantics

## Design Principle

Keep descriptors:

- deterministic
- easy to validate
- readable enough for contributors
- strict enough for cheap parsing

TOML is the only supported descriptor format.
