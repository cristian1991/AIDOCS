# Docs Site Structure Proposal

This document proposes the AIDOCS docs information architecture for the future hosted docs surface at:

- `docs.codenexus.cloud/aidocs`

It is a structure proposal, not a full site implementation.

## Positioning

- `codenexus.cloud` remains the umbrella marketing site for CodeNexus products.
- `docs.codenexus.cloud/aidocs` should be the product-specific documentation surface for AIDOCS.
- AIDOCS docs should not read like a sub-feature page of AutoDeployBase.

## Goals

- Keep the landing path simple for new users.
- Separate product overview from operator/reference detail.
- Make host differences clear without scattering them across unrelated pages.
- Keep benchmark, CLI, and runtime docs easy to find.

## Proposed Top-Level Navigation

1. Overview
2. Install
3. Core Concepts
4. CLI
5. MCP Runtime
6. Host Integrations
7. Benchmarks
8. Reference
9. Roadmap

## Proposed Page Tree

### 1. Overview
- `overview/index`
  - What AIDOCS is
  - Core vs MCP vs CLI
  - Supported host types
  - When to use AIDOCS

### 2. Install
- `install/index`
  - Quick install paths
  - Core-only vs Core+MCP
- `install/core-routing`
  - global routing files
  - commands
  - plugin/hook bootstrap
- `install/mcp-runtime`
  - Python package install
  - `.mcp.json`
  - MCP runtime activation
- `install/troubleshooting`

### 3. Core Concepts
- `concepts/memory-model`
  - `/.MEMORY/`
  - routers
  - sessions
  - durable vs session-local information
- `concepts/managed-mode`
  - `/aidocs`
  - managed mode lifecycle
  - session binding
- `concepts/routing-and-retrieval`
  - classify
  - route
  - orchestrate
  - direct inspection vs MCP-first work

### 4. CLI
- `cli/index`
  - command overview
- `cli/init`
- `cli/status`
- `cli/sync`
- `cli/benchmark`
- `cli/version`

### 5. MCP Runtime
- `mcp/index`
  - runtime model
  - tool model
- `mcp/unified-tool-model`
  - investigate/find/trace/bundle/schema
- `mcp/advanced-surfaces`
  - git analysis
  - execution evidence
  - capability inspection
  - procedures as optional structure

### 6. Host Integrations
- `hosts/index`
  - support matrix
- `hosts/claude-code`
  - hook path
  - runtime-driven classify + route
- `hosts/opencode`
  - plugin path
  - command-aware and advisory classification behavior
  - current capability caveats
- `hosts/generic-mcp-clients`

### 7. Benchmarks
- `benchmarks/index`
  - benchmark purpose
  - public scenario sets
- `benchmarks/public-contract`
  - scenario-set rules
  - output schema
  - public vs private guidance
- `benchmarks/automation`
  - CI artifact capture
  - how to compare runs

### 8. Reference
- `reference/config`
  - `aidocs.toml`
  - `aidocs-plugin.json`
  - `action_tokens`
- `reference/commands`
  - `/aidocs`
  - `/reingest`
  - `/archive`
  - `/personality`
  - `/clean`
- `reference/files-and-layout`
  - project layout
  - generated/runtime files

### 9. Roadmap
- `roadmap/index`
  - current release line
  - `v1.2.0` focus
  - future workstreams

## Source Mapping From Current Repo Docs

- `README.md` -> Overview + entry navigation
- `README_INSTALL.md` -> Install
- `mcp/README.md` -> MCP Runtime
- `mcp/HOST_INTEGRATION.md` -> Host Integrations
- `mcp/BENCHMARKS.md` -> Benchmarks
- `mcp/ROADMAP.md` -> Roadmap

## Content Rules

- Keep the overview pages product-first.
- Push deep runtime detail into MCP/Host/Reference pages.
- Keep OpenCode caveats explicit and localized to host docs.
- Keep procedures demoted unless they become a stronger product surface later.
- Keep benchmark docs honest: no vanity benchmark framing.

## Notes For CodeNexus Docs Split

- `docs.codenexus.cloud/aidocs` should feel complete on its own.
- Cross-link to AutoDeployBase where relevant, but do not make AIDOCS docs depend on ADB context.
- If shared CodeNexus concepts exist, they should live in a clearly shared layer, not in product-specific docs by accident.
