#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$RUNTIME_ROOT/.." && pwd)"
SERVER_ROOT="$REPO_ROOT/mcp/server"

if [[ ! -d "$SERVER_ROOT" ]]; then
  echo "AIDOCS MCP server package not found: $SERVER_ROOT" >&2
  exit 1
fi

if [[ -n "${PYTHONPATH:-}" ]]; then
  export PYTHONPATH="$SERVER_ROOT:$PYTHONPATH"
else
  export PYTHONPATH="$SERVER_ROOT"
fi

PYTHON_BIN=""
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_BIN" ]]; then
  echo "python3/python/py not found in PATH" >&2
  exit 1
fi

exec "$PYTHON_BIN" -m aidocs_mcp.claude_hook
