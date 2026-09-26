# File-Backed Service Interfaces

## SessionStore

Responsibilities
- enumerate session folders
- parse `SESSION.md`
- create session folder from templates
- update structured sections in `SESSION.md`
- resolve linked session-local files (`context.md`, `plans/`, `agents/`, `artifacts/`)

Suggested operations
- `list_sessions(project_root)`
- `read_session(project_root, session_id)`
- `create_session(project_root, session_data)`
- `update_session(project_root, session_id, patch)`

## MemoryStore

Responsibilities
- read canonical memory files
- write durable facts/rules to canonical memory files
- preserve managed/user boundaries where required

Suggested operations
- `read_memory(project_root, targets)`
- `capture_memory(project_root, kind, content, target_hint=None)`

## ManagedFileService

Responsibilities
- split file at managed marker
- rewrite managed section only
- preserve user section exactly

Suggested operations
- `has_marker(path)`
- `rewrite_managed_section(path, managed_content)`
- `read_user_section(path)`
