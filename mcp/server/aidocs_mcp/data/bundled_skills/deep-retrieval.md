---
name: deep-retrieval
description: Prefer deeper retrieval and precision tools before editing or concluding behavior.
kind: reasoning
tags: retrieval, precision, investigation
---

# deep-retrieval

Use when the task needs exact signatures, constructors, enum values, service APIs, ownership boundaries, or workflow tracing before making changes.

Do:
- prefer exact indexed retrieval before raw file reads
- get exact method signatures before calling or editing APIs
- trace data flow or workflow before changing behavior you do not fully understand
- confirm file ownership and lane scope before editing conductor-managed work

Do not:
- guess an API shape from nearby code
- broad-read files when a focused indexed query can answer first
- conclude behavior from names alone without checking indexed evidence
