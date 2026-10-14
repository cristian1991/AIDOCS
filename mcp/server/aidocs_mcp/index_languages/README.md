# Index Language Descriptors

This directory contains the built-in TOML language descriptors used by AIDOCS indexing.

## Current Model

- Built-in descriptors ship here.
- Projects can add their own descriptors in a project-local `index_languages/` directory.
- Project-local descriptors override built-in descriptors when they share the same language name or extensions.
- Descriptor files are loaded once and cached in memory for low-overhead indexing.

## Supported Keys

Minimal example:

```toml
name = "dart"
extensions = [".dart"]
```

Current supported keys:

- `name`
- `extensions`
- `suffixes`
- `include_globs`

## Goals

This is the first implementation slice for user-extensible indexing.

What it supports now:

- file discovery by extension
- file discovery by suffix
- file discovery by glob

What comes later:

- support tiers
- heuristic outline rules
- module/role hints
- richer semantics

## Notes

- Keep descriptor files simple and deterministic.
- TOML is the only supported descriptor format.
- Descriptor files themselves are not indexed as source files.
