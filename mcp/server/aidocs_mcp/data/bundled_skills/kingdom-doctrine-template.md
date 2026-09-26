---
name: kingdom-doctrine-template
description: Scaffold for one project's own law (its `kingdom-doctrine`). Inherits `empire-doctrine` and holds only project-specific invariants, authority, data and release boundaries. Copy it, fill it, and register it as `kingdom-doctrine` — this scaffold itself binds no one.
kind: sample
tags: sample, doctrine, project, kingdom, invariants, template
---

# kingdom-doctrine-template

This is a SCAFFOLD for a project's own law. It binds no one as shipped.

Every project governed by the Empire inherits `empire-doctrine` (portable, cross-project law) and owns ONE project scroll registered under the id `kingdom-doctrine`. That id is reserved for the project's actual law and is never shipped: a bundled body under that name would outrank the project's own scroll by address, which is law substitution without a law write.

To adopt it: copy this file to `.MEMORY/skills/kingdom-doctrine.md` in your project, set `name: kingdom-doctrine` and `kind: doctrine`, fill the sections below, and delete every line that merely restates Empire law. The Empire's own repository uses `aidocs-doctrine` as its project scroll instead of a generic kingdom body.

Editorial rules for the filled scroll:

- Doctrine states what must remain true, independent of current tool names. Current commands, flags and file layouts belong in a runbook.
- Project law ADDS or TIGHTENS. It never copies, paraphrases or weakens `empire-doctrine`; a second wording of the same rule is a second authority.
- One rule has one authoritative wording. Reference it elsewhere; do not restate it.

## I. Project identity

State the smallest durable description needed to keep agents from solving the wrong product.

- product/system purpose:
- primary users/surfaces:
- authoritative runtime(s):
- important external contracts:

## II. Architecture invariants

List only architecture rules whose violation would create a real product, safety, migration, or maintenance defect.

Examples of valid project law:

- one named service owns a security decision;
- a public API must remain backward compatible across a specified boundary;
- a data store is canonical while another is a rebuildable projection;
- a generated artifact must never become hand-authored authority.

Do not put current file paths, helper names, or incidental implementation layout here unless the path itself is part of the contract.

## III. Security and authority boundaries

Record project-specific authority that is not already portable Empire law.

- privileged actors/roles:
- destructive-action boundaries:
- tenant/user isolation constraints:
- secrets/private-data boundaries:
- project-specific fail-closed requirements:

## IV. Data and state invariants

Define canonical ownership and allowed projections/caches.

For each critical state family, make clear:

- canonical authority;
- derived/rebuildable copies;
- required synchronization or invalidation behavior;
- what happens when a dependency is unavailable.

## V. Integration and compatibility invariants

Record the seams that external consumers depend on: protocols, APIs, schemas, event contracts, persisted formats, or host capabilities.

A compatibility layer may exist, but it must be named as such and have an explicit retirement/ownership rule if temporary.

## VI. Project release/deploy law

State project-specific release invariants only, such as:

- what artifact/commit identity a release binds to;
- which proof boundary is authoritative;
- what must be reproducible or signed;
- what public/private evidence may be emitted.

Current commands and flags belong in a runbook.

## VII. Project-specific work discipline

Add only workflow requirements that arise from this project's architecture or risk profile. Empire backlog, memory, evidence, completion, and failure-stewardship law already applies and must not be restated here.

## VIII. Exceptions and supersession

Every project-law exception must name:

- the Empire/project rule it narrows or extends;
- why the project needs the exception;
- its scope;
- whether it is permanent or has an expiry/removal condition.

A silent contradiction is invalid project law.
