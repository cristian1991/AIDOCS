#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE="check"
TARGET=""

if [ "$#" -gt 0 ]; then
  case "$1" in
    check|fix)
      MODE="$1"
      shift
      ;;
  esac
fi

if [ "$#" -gt 0 ]; then
  TARGET="$1"
fi

if command -v pwsh >/dev/null 2>&1; then
  PS_CMD="pwsh"
elif command -v powershell >/dev/null 2>&1; then
  PS_CMD="powershell"
else
  echo "PowerShell not found. Install pwsh (recommended) or powershell." >&2
  exit 2
fi

if [ -n "$TARGET" ]; then
  exec "$PS_CMD" -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/check-memory-drift.ps1" -Mode "$MODE" -ScanRoot "$TARGET"
else
  exec "$PS_CMD" -NoProfile -ExecutionPolicy Bypass -File "$SCRIPT_DIR/check-memory-drift.ps1" -Mode "$MODE"
fi
