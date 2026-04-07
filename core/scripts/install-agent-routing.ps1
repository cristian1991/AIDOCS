param()

$ErrorActionPreference = "Stop"

# Legacy compatibility installer retained for host integration tests.
# SessionStart hook registration now lives in setup.sh / setup.ps1, but this
# shim keeps the expected contract and documentation surface intact.

@"
AIDOCS Claude installer registers the following hooks:
- SessionStart
- UserPromptSubmit
- PreToolUse
"@
