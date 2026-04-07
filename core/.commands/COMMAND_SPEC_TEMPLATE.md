---
description: <one-line command description>
command_id: <slash-command-name-without-slash>
preferred_executor: <mcp|host|advisory>
allow_uninitialized: <true|false>
---

## Intent
- State the command goal in one sentence.

## Inputs
- Define required inputs.
- Define default input resolution.

## Preconditions
- List conditions that MUST already be true.
- If none, say `- None`.

## Primary Execution
1. State each required action in order.
2. Use imperative wording.
3. Prefer exact tool or runtime names when known.

## Branching
- If `<condition>`: do `<action>`.
- Else if `<condition>`: do `<action>`.
- Else: do `<action>`.

## STOP Conditions
- Stop when `<condition>`.
- Ask exactly one targeted question when user choice is required.

## Output
- List the required user-facing outputs in priority order.
- State default rendering preferences if needed.

## Fallback
- Define the non-MCP or degraded-mode path.
- Keep the same user-facing contract unless impossible.

## Rules
- List command-specific MUST or MUST NOT rules.
- Keep these short and testable.

## Arguments
$ARGUMENTS
