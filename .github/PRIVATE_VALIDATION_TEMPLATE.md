# Private Validation Template

This document describes the intended maintainer-triggered private validation flow for AIDOCS.

It is not a public PR workflow.

It exists to protect:

- private tests
- private fixtures
- private benchmark corpora
- protected behavioral knowledge encoded in the private repo

## Purpose

Use private validation after public PR CI has completed and a maintainer decides the change is worth deeper verification.

## Hard Rules

- Do not run this automatically for arbitrary fork PRs.
- Do not expose private tests to public contributors.
- Do not post detailed private failures back to public PRs.
- Do not execute public PR code in a privileged workflow without explicit maintainer control.

## Safe Model

### Step 1: Public PR CI runs first
- unprivileged
- no secrets
- no private test content

### Step 2: Maintainer reviews the PR
- decide whether private validation is needed
- only then trigger the private flow

### Step 3: Private repo validates the candidate change
- apply the candidate diff in a private branch/worktree
- run private tests and private benchmark corpora
- keep results internal

### Step 4: Report only coarse result externally
- `private validation passed`
- or `private validation failed; maintainer review required`

## Recommended Trigger Options

Choose one of these:

### Option A: Maintainer cherry-pick branch
- maintainer creates a temporary private branch from the public PR diff
- private CI runs on that branch

### Option B: Maintainer patch import
- maintainer-approved tool imports the PR patch into a private worktree
- private CI runs there

### Option C: Manual dispatch with explicit PR/ref input
- private workflow uses `workflow_dispatch`
- maintainer pastes PR number or trusted ref
- workflow fetches the diff safely into private validation context

## Reporting Guidance

Allowed to report publicly:

- pass/fail
- high-level category such as `private regression failed`
- high-level category such as `private benchmark regression detected`

Do not report publicly:

- test names
- fixture details
- benchmark corpus prompts
- internal stack traces that reveal protected behavior
- exact expected outputs from private tests

## Example Outcome Language

- `Public CI passed. Private validation pending maintainer review.`
- `Private validation passed.`
- `Private validation failed in internal regression checks; maintainer review required.`

## Summary

Public PR CI proves public compatibility.

Private validation protects the project's private behavioral knowledge.

Both are necessary, but they must remain separate.
