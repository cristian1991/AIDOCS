# Built-in Skills

Built-in AIDOCS skills live in this directory.

Each skill is a small markdown descriptor with frontmatter.

Current keys:

- `name`
- `description`
- `tags`
- `kind`

Recommended `kind` values:

- `helper`
- `reasoning`
- `verification`
- `authoring`

Rules:

- built-in skills should guide reasoning, not own orchestration
- workflow authority belongs in runtime/conductor code, not skill prose
- keep skill text compact and high-signal; prefer `Use when`, `Do`, and `Do not`

Project-local skills can be added in:

```text
<project-root>/.MEMORY/skills/*.md
```
