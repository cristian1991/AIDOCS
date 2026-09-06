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

## Reference Patterns & Ref-Integrity (2026-06-20)

Descriptors can declare USAGE patterns whose captured token must resolve to a
definition — the seam for project-custom functions like a bespoke
`@lang.t("key")` SSR-autotranslation call.

```toml
# tokens this descriptor's files REFERENCE (capture group = the token)
reference_patterns = [
  { pattern = '@lang\.t\("([^"]+)"\)', kind = "i18n_key", capture = 1 },
]

# where tokens of each reference kind are DEFINED
[definition_source.i18n_key]
symbol_kind = "i18n_key"            # resolve vs code_outlines of this kind
# — or, for tokens defined in resource files —
# glob = "resources/**/*.resx"
# pattern = '<data name="([^"]+)"'
# capture = 1
```

Reference tokens are stored in the `code_references` index table, populated in
the SAME extraction pass as `code_outlines`. The ref-integrity report —
references whose token has no resolving definition — is read-only:

- MCP: `ai_slop(mode="broken_refs")`  (Tier-R, bounded, heuristic-labeled)

Truth labels: extraction is HEURISTIC regex, not an AST; only reference kinds
with a declared `definition_source` are resolvable (others report
`resolvable: false` — never a false "broken" claim).

**Worked example:** `mcp/server/aidocs_mcp/data/samples/refintegrity/` ships a runnable
custom-`@lang.t("key")` language — keys defined in `keys.dview`, used in `page.dview`; the
undefined `footer.copyright` is flagged broken. Proven by `mcp/tests/indexing/test_refintegrity_sample.py`.

### Safety (memory-poisoning law)

Project-local descriptors are untrusted EVIDENCE that drive indexing only —
never auto-promoted to law/doctrine. Python `re` has no execution timeout, so
descriptor regex is screened at validation (compile + nested-quantifier ReDoS
heuristic + length bound) and caged at runtime (per-line scan, line-length
clamp, capped match count). Unsafe or malformed `reference_patterns` /
`definition_source` make the descriptor invalid.

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
- reference patterns + definition_source → `code_references` table + ref-integrity (`ai_slop` mode `broken_refs`)

Still future work:

- richer support-tier-aware behavior in retrieval/ranking
- more semantic shorthand/tag families
- community descriptor packs
- more advanced declarative semantics

## Worked Example: Custom i18n Reference Integrity (dentalview)

A complete, runnable sample lives at:

```
mcp/server/aidocs_mcp/data/samples/refintegrity/
├── index_languages/dentalview.toml   # project-local descriptor
└── app/
    ├── keys.dview                     # defines i18n keys
    └── page.dview                     # uses @lang.t("...")
```

**Descriptor** (`dentalview.toml`):
```toml
language = "dentalview"
extensions = [".dview"]
tier = "heuristic"
role_hints = ["template", "i18n"]

reference_patterns = [
  { pattern = '@lang\.t\("([^"]+)"\)', kind = "i18n_key", capture = 1 },
]

[definition_source.i18n_key]
symbol_kind = "i18n_key"
```

**Keys** (`keys.dview`):
```dview
# i18n key definitions for the demo app
key = "greeting.hello"
key = "greeting.goodbye"
key = "nav.home"
key = "nav.about"
```

**Template** (`page.dview`):
```dview
# Page template — i18n via @lang.t
<h1>@lang.t("greeting.hello")</h1>
<p>@lang.t("nav.home")</p>

# This key is NOT defined in keys.dview — expect broken reference
<footer>@lang.t("footer.copyright")</footer>
```

**Run the ref-integrity check:**
```bash
# In a temp project copy (hermetic, no repo artifacts)
python -m pytest mcp/tests/indexing/test_refintegrity_sample.py -v
```

**Expected output:**
- Descriptor validates successfully
- `find_broken_references` reports exactly 1 broken i18n_key: `footer.copyright`
- Evidence kind is `heuristic` (regex extraction, not AST)

This demonstrates the full loop: define a custom language, declare its reference
patterns, provide a definition source, and let AIDOCS flag dangling references
without false positives.

## Design Principle

Keep descriptors:

- deterministic
- easy to validate
- readable enough for contributors
- strict enough for cheap parsing

TOML is the only supported descriptor format.
