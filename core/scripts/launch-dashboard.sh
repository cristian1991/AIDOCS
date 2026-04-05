#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AIDOCS_PATH="${AIDOCS_PATH:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
DASHBOARD_DIR="$AIDOCS_PATH/apps/aidocs-dashboard"

if [[ ! -d "$DASHBOARD_DIR" ]]; then
  echo "Dashboard not found at $DASHBOARD_DIR"
  exit 1
fi

if [[ -f "$DASHBOARD_DIR/src-tauri/target/release/aidocs-dashboard" ]]; then
  "$DASHBOARD_DIR/src-tauri/target/release/aidocs-dashboard" &
else
  echo "Starting in dev mode..."
  cd "$DASHBOARD_DIR"
  npm run tauri dev
fi
