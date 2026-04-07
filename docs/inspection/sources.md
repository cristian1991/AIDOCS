# Workflow Inspection Sources

## Install / Setup Sources
- `core/scripts/install-agent-routing.cmd`
- `core/scripts/install-agent-routing.ps1`
- `core/scripts/install-agent-routing.sh`
- `README_INSTALL.md`
- `README.md`

## Project Setup / MCP Sources
- `mcp/server/aidocs_mcp/runtime_service.py`
- `mcp/server/aidocs_mcp/mcp_server.py`
- `mcp/server/aidocs_mcp/managed_mode_service.py`
- `mcp/server/aidocs_mcp/session_store.py`
- `core/.commands/aidocs.md`

### Runtime entrypoints traced so far
- `RuntimeService.project_init(...)`
- `RuntimeService.ensure_claude_mcp_config(...)`
- `RuntimeService.project_bootstrap_or_resume(...)`
- `RuntimeService.session_start(...)`
- `RuntimeService.aidocs_orchestrate(...)`
- `RuntimeService._sync_bootstrap_indexes(...)`
- `ManagedModeService.set_mode(...)`

## Claude Host Sources
- `mcp/server/aidocs_mcp/claude_hook.py`
- `mcp/HOST_INTEGRATION.md`

### Claude hook entrypoints traced so far
- `ClaudeHookHandler.handle(...)`
- `ClaudeHookHandler._handle_aidocs_command(...)`
- `ClaudeHookHandler._handle_session_start(...)`
- `ClaudeHookHandler._handle_user_prompt_submit(...)`
- `ClaudeHookHandler._handle_pre_tool_use(...)`
- `ClaudeHookHandler._build_lightweight_prompt_context(...)`

## OpenCode Host Sources
- `core/plugins/aidocs.js`
- `mcp/HOST_INTEGRATION.md`

### OpenCode plugin entrypoints traced so far
- `runAidocsHostState(...)`
- `resolveFilesystemAidocsState(...)`
- `resolveAidocsState(...)`
- `resolvePromptHostState(...)`
- `buildPromptContext(...)`
- `AIDOCSPlugin()['chat.message']`
- `AIDOCSPlugin()['experimental.chat.system.transform']`
- `AIDOCSPlugin()['experimental.chat.messages.transform']`
- `AIDOCSPlugin()['command.execute.before']`
- `AIDOCSPlugin()['tool.execute.before']`
- `AIDOCSPlugin()['tool.execute.after']`
- `AIDOCSPlugin()['shell.env']`

## State Files To Trace
- `/.MEMORY/config/aidocs-managed.json`
- `/.mcp.json`
- `/.MEMORY/.aidocs/index.aidocs`
- `/.MEMORY/INDEX.md`
- `/.MEMORY/sessions/<session-id>/SESSION.md`
