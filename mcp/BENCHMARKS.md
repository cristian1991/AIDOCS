# Benchmarks

This document defines the public benchmark contract for AIDOCS `v1.2.0` work.

The goal is not to publish vanity numbers. The goal is to create repeatable, realistic benchmark scenarios that help evaluate retrieval quality, classification behavior, and operator-facing runtime performance.

## Principles

- Use realistic prompts, not toy prompts.
- Prefer vague, messy, user-style requests over over-optimized synthetic phrasing.
- Measure useful surfaces: sync, classification, retrieval, schema access, and indexed-vs-raw comparison.
- Keep the public benchmark set safe to publish.
- Keep any sensitive or proprietary benchmark corpora private.

## CLI Entry Point

```bash
aidocs benchmark [path] [--json] [--iterations N] [--scenario-set public] [--out result.json]
```

- Use `--out <file>` to save the full JSON benchmark payload to disk.
- `--out` works with both human-readable and `--json` terminal modes.

## Current Public Scenario Set

The default scenario set is `public`.

It currently includes:

- multilingual classification prompts
- code retrieval scenarios
- schema retrieval scenarios when schema entities are available
- comparative indexed-vs-raw file-search scenarios

## What the Benchmark Reports

### `sync`
- memory sync counts
- code file/module counts
- schema entity count
- elapsed time

### `classification`
- total prompt count
- total classifications
- elapsed time
- classifications per second
- overall action-kind counts
- per-language action-kind counts

### `retrieval`
- code-oriented retrieval scenario timings
- result-size counts per scenario

### `schema_benchmark`
- schema-oriented retrieval scenario timings
- result-size counts per scenario
- may be empty on projects with no schema coverage

### `comparative`
- indexed AIDOCS retrieval timing
- naive raw file-scan timing
- raw scanned-file counts

## Public vs Private Benchmark Content

### Public benchmark content
- benchmark harness code
- public scenario sets such as `public`
- realistic but non-sensitive prompts
- project-agnostic retrieval scenarios
- JSON output schema and usage docs

### Private benchmark content
- prompts derived from internal operator behavior when they reveal sensitive workflows
- corpora based on private repositories or proprietary architectures
- private release-gating baselines
- internal-only scenario sets such as a future `private`

## Benchmark Design Rules

- Do not claim benchmark "accuracy" unless there is a real labeled evaluation method.
- Do not design prompts only to make AIDOCS look good.
- Do not compare AIDOCS against an unrealistically weak baseline and present that as scientific proof.
- Raw baseline comparisons should represent plausible non-AIDOCS agent behavior such as naive repo scanning.
- When a project lacks schema coverage, schema scenarios should report zero coverage gracefully instead of failing.

## Future Expansion

- add richer multilingual batches and mixed-language scenarios
- add public sample-project benchmark fixtures
- add optional export/report files
- add private-only scenario sets in the private repo
- add benchmark documentation pages under the future docs site

## Recommended Usage

Human-readable run:

```bash
aidocs benchmark . --iterations 10
```

Machine-readable run:

```bash
aidocs benchmark . --json --iterations 10 --scenario-set public
```

## Automation

- The repo can run the public benchmark set in CI and upload the JSON result as a workflow artifact.
- Benchmark artifacts should use the public scenario set unless the run is explicitly private/internal.
- CI benchmark artifacts are for trend visibility and regression spotting, not for marketing claims without human review.
