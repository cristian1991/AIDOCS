#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET="${1-}"

if command -v pwsh >/dev/null 2>&1; then
  PS_CMD="pwsh"
elif command -v powershell >/dev/null 2>&1; then
  PS_CMD="powershell"
else
  echo "PowerShell not found. Install pwsh (recommended) or powershell." >&2
  exit 2
fi

if [ -n "$TARGET" ]; then
  exec "$PS_CMD" -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/detect-memory-loop.ps1" -ProjectRoot "$TARGET"
else
  exec "$PS_CMD" -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/detect-memory-loop.ps1"
fi
