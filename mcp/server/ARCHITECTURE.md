# MCP Server Architecture

## Core design

- Canonical source of truth:
  - `build/` for AIDOCS contracts/templates
  - project-local `/.MEMORY/**` for runtime and durable memory
- MCP server:
  - validates and enforces lifecycle rules
  - performs structured file-backed reads/writes
- Future index:
  - SQLite only
  - derived only
  - rebuildable from files and code

## Layers

### 1. File services
- session file read/write
- memory file read/write
- managed-file split/merge helpers

### 2. Domain services
- session lifecycle
- memory capture
- archive behavior
- project update/init helpers

### 3. MCP tool layer
- method registration
- argument validation
- structured outputs

## First concrete build target

Implement session-aware file services first, before any MCP framework binding.
