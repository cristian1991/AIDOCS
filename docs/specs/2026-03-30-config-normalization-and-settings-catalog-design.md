# Config Normalization And Settings Catalog Design

## Goal

Standardize AIDOCS configuration for `v1.9.0` so that:

- config values stay simple and readable
- scope precedence is handled centrally by the runtime
- runtime state is clearly separated from config
- every meaningful setting has machine-readable metadata for validation, comments, and future GUI rendering

## Core Principle

Do not over-model configuration.

The actual config files should stay simple.
The schema metadata should be separate.
Scope precedence should be handled once by the config engine, not repeated inside every setting object.

## Config Scopes

Three editable config scopes plus runtime state:

1. `global`
2. `project`
3. `session`
4. `runtime state` (non-editable)

### Precedence

1. `session`
2. `project`
3. `global`
4. built-in defaults

## Actual Config Files Must Stay Clean

Example values should look like this:

```json
{
  "skills": {
    "activation_mode": "auto"
  }
}
```

Project config override:

```json
{
  "skills": {
    "activation_mode": "selected-only"
  }
}
```

Session config example:

```json
{
  "skills": {
    "selected": [
      "systematic-debugging"
    ]
  }
}
```

These files should contain only the actual configured values, not embedded metadata.

## Settings Catalog

Metadata should live in a separate settings catalog.

The catalog should be flat and readable, keyed by dotted setting path.

Example:

```json
{
  "skills.activation_mode": {
    "type": "enum",
    "default": "auto",
    "allowed_values": ["auto", "selected-only", "off"],
    "description": "Controls how AIDOCS activates skills.",
    "value_descriptions": {
      "auto": "Activate skills automatically from runtime intent and workflow state.",
      "selected-only": "Only activate explicitly selected skills.",
      "off": "Disable automatic skill activation."
    },
    "allowed_scopes": ["global", "project", "session"],
    "agent_editable_scopes": ["project", "session"],
    "security_sensitive": false,
    "requires_restart": false
  }
}
```

## Why This Model

This gives us:

- clean config files
- one source of truth for setting descriptions
- validation rules
- GUI-ready metadata
- generated comments/help text later

without making the actual config values heavy or hard to reason about.

## Config Domains

The normalized config model should include at least:

- `providers`
- `skills`
- `conductor`
- `runtime`
- `hosts`
- `indexing`
- `verification`
- `security`
- `debug`

## Runtime State Is Separate

These remain runtime state and are not editable config:

- active skills
- triggered skills
- provider compatibility evaluation result
- prompt intent / prompt-state activation result
- conductor live lane state
- host-state snapshots
- compiled workflow artifacts

This separation must stay strong.

## Security Rules For Config

### Editable under controlled conditions

Normal config may be agent-editable only when explicitly allowed, for example:

- explicit user request
- development workflows where config edits are intentionally enabled

### Never editable by agents

Security settings must remain non-editable by agents regardless of mode.

This includes:

- security config domain itself
- GUI/control-plane mutation settings
- any hardcoded protection policy files

### Release rule

Self-edit mode must not ship in release builds.

If it exists at all, it should only exist in:

- development branches
- or development-only builds/profiles

## Suggested Domains And Examples

### Providers

Examples:

- bundled provider enabled/disabled
- compatibility override policy

### Skills

Examples:

- activation mode
- selected defaults
- override behavior

### Conductor

Examples:

- reopen-on-fullsuite-failure
- strict file-overlap blocking
- ownership persistence

### Hosts

Examples:

- directive style
- startup context once
- host-specific bootstrap behavior

### Indexing

Examples:

- git update sync mode
- delta threshold
- extra skip dirs

### Verification

Examples:

- require full suite before final completion
- lane review strictness

### Security

Examples:

- protected files/directories/extensions/globs
- allow config edits with approval
- GUI agent access policy

## Immediate Problem Areas To Normalize

Current known inconsistencies to resolve:

- `aidocs.toml` and `aidocs-plugin.json` overlap
- `/.MEMORY/skill-providers.json` is in the wrong location for policy/config
- `aidocs-managed.json` is binding/runtime state, not true config
- `workflow-actions.json` is compiled artifact, not policy config
- plugin defaults still duplicate runtime policy assumptions

## Success Criteria

This config model is successful when:

- config values remain simple
- metadata is complete enough for validation and GUI rendering
- scope precedence is centralized in code
- runtime state is not confused with config
- comments/help can be generated from the settings catalog
- AIDOCS behavior becomes easier to understand and safer to change
