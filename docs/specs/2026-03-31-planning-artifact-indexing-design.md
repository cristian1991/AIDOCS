# Roadmap/Spec/Plan Indexing And Retrieval Design

## Goal

Make roadmap, spec, and plan artifacts first-class indexed targets in AIDOCS, with retrieval quality comparable to code retrieval and editing ergonomics that reduce hard-patch fragility.

## Core Problem

Planning artifacts currently suffer from:
- Weaker retrieval than code files
- Brittle editing/update patterns
- Frequent hard-patch failures when updating structured planning docs
- Too much raw-file fallback for planning work
- No structured indexing of roadmap sections, spec sections, plan phases/lanes/tasks

## Design Principles

1. **First-class indexing**: Planning artifacts should be indexed with the same rigor as code
2. **Structured retrieval**: Sections, phases, lanes, and tasks should be directly queryable
3. **Safe editing**: Updates should preserve structure and avoid patch failures
4. **Less raw-file fallback**: Agents should use indexed retrieval for planning work

## Indexing Model

### What gets indexed

- Roadmap files
- Spec files
- Plan files
- Session plan files
- Handoff files

### Index structure

```
PlanningIndex:
  - documents:
    - id
    - type (roadmap | spec | plan | session_plan | handoff)
    - path
    - sections:
      - title
      - content_summary
      - structured_data (phases, lanes, tasks, etc.)
  - tasks:
    - id
    - parent_document_id
    - status
    - assigned_to (lane/agent)
    - files
  - lanes:
    - id
    - parent_document_id
    - files
    - status
```

## Retrieval API

### Query operations

- `planning_find(query, type=None)` - find planning documents
- `planning_get_section(doc_id, section_title)` - get specific section
- `planning_get_task(task_id)` - get task details
- `planning_get_lane(lane_id)` - get lane details
- `planning_search(text, type=None)` - full-text search across planning artifacts

### Update operations

- `planning_update_section(doc_id, section_title, content)` - safe section update
- `planning_update_task(task_id, updates)` - safe task update
- `planning_update_lane(lane_id, updates)` - safe lane update

## Editing Ergonomics

### Current problems

- Hard-patch failures when structure changes
- Manual line-number tracking
- Fragile string matching
- No structural validation

### Proposed solution

- Structural editing API that understands planning document format
- Validation before write
- Automatic structure preservation
- Conflict detection for concurrent edits

## Success Criteria

Planning artifact indexing is complete when:
- Roadmap/spec/plan files are indexed with structured sections
- Tasks, lanes, and phases are directly queryable
- Safe editing API reduces patch failures
- Agents use indexed retrieval for planning work by default
- Raw-file fallback is rare and only for exceptional cases
