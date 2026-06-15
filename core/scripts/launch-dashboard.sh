#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
AIDOCS_PATH="${AIDOCS_PATH:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
AIDOCS_HOME="${HOME}/.aidocs"

if [[ -f "$AIDOCS_HOME/aidocs-dashboard" ]]; then
  "$AIDOCS_HOME/aidocs-dashboard" &
elif [[ -f "$AIDOCS_PATH/apps/aidocs-dashboard/src-tauri/target/release/aidocs-dashboard" ]]; then
  "$AIDOCS_PATH/apps/aidocs-dashboard/src-tauri/target/release/aidocs-dashboard" &
else
  echo "AIDOCS Dashboard not found."
  echo ""
  echo "Expected locations:"
  echo "  $AIDOCS_HOME/aidocs-dashboard"
  echo "  $AIDOCS_PATH/apps/aidocs-dashboard/src-tauri/target/release/aidocs-dashboard"
  echo ""
  echo "Build from source:"
  echo "  cd $AIDOCS_PATH/apps/aidocs-dashboard && npm install && npm run tauri:build"
  echo "  (tauri:build prunes stale debug/incremental artifacts after a successful build)"
  echo ""
  echo "Or download a release from:"
  echo "  https://github.com/cristian1991/AIDOCS/releases"
  exit 1
fi
