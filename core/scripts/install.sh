#!/usr/bin/env bash
# AIDOCS Universal Installer — zero dependencies, one command
# Usage: curl -fsSL https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.sh | bash
set -euo pipefail

AIDOCS_HOME="${AIDOCS_HOME:-$HOME/.aidocs}"
# Pinned to the AIDOCS-blessed CPython (runtime_provisioner.PINNED / _PBS_*).
# The bootstrap archive is SHA-256 verified below against the per-platform pin
# (keep these in lockstep with mcp/server/aidocs_mcp/runtime_provisioner.py).
PYTHON_VERSION="3.12.13"
PYTHON_BUILD_TAG="20260510"

# ── Detect platform ──
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64)  PLATFORM="x86_64-unknown-linux-gnu";  PYTHON_SHA256="e7332b4b4bb85006deb48d251c786a04c14de104c9b3a006b33457a4a604b8bc" ;;
      aarch64) PLATFORM="aarch64-unknown-linux-gnu"; PYTHON_SHA256="87097de12bc212e41ea8409efd0083fe06465d725e35d130e4007a4bf7e4f1c8" ;;
      *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    ;;
  Darwin)
    case "$ARCH" in
      x86_64)  PLATFORM="x86_64-apple-darwin";  PYTHON_SHA256="cd369e76973c3179bc578230d8615ab621968ed758c5e32f636eecef4ad79894" ;;
      arm64)   PLATFORM="aarch64-apple-darwin"; PYTHON_SHA256="5a30271f8d345a5b02b0c9e4e31e0f1e1455a8e4a04fba95cd9762472abc3b17" ;;
      *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    ;;
  *) echo "Unsupported OS: $OS (use the Windows installer instead)"; exit 1 ;;
esac

PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_TAG}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_TAG}-${PLATFORM}-install_only.tar.gz"

echo ""
echo "  AIDOCS Installer"
echo "  ═════════════════"
echo ""
echo "  Platform: $OS $ARCH"
echo "  Install:  $AIDOCS_HOME"
echo ""

# ── Check for existing install ──
if [ -d "$AIDOCS_HOME/python" ]; then
  echo "  Existing installation found at $AIDOCS_HOME"
  # curl | bash loses the controlling TTY — auto-approve so the one-liner
  # doesn't silently hang waiting on read. Users who want to keep the old
  # tree unset AIDOCS_ASSUME_YES and run the script directly.
  if [ "${AIDOCS_ASSUME_YES:-}" = "1" ] || [ ! -t 0 ]; then
    echo "  Non-interactive session — reinstalling."
    REPLY="y"
  else
    read -p "  Reinstall? [y/N] " -n 1 -r
    echo
  fi
  if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "  Cancelled."
    exit 0
  fi
  rm -rf "$AIDOCS_HOME/python"
fi

mkdir -p "$AIDOCS_HOME"

# ── Download + extract Python ──
echo "  [1/4] Downloading Python $PYTHON_VERSION..."
TMPDIR="$(mktemp -d)"
trap "rm -rf $TMPDIR" EXIT

curl -fsSL "$PYTHON_URL" -o "$TMPDIR/python.tar.gz"
# ── SHA-256 verification (no unverified bootstrap interpreter) ──
if command -v sha256sum >/dev/null 2>&1; then
  GOT_SHA="$(sha256sum "$TMPDIR/python.tar.gz" | cut -d' ' -f1)"
elif command -v shasum >/dev/null 2>&1; then
  GOT_SHA="$(shasum -a 256 "$TMPDIR/python.tar.gz" | cut -d' ' -f1)"
else
  echo "  ERROR: no sha256sum/shasum available to verify the Python download — refusing."; exit 1
fi
if [ "$GOT_SHA" != "$PYTHON_SHA256" ]; then
  echo "  ERROR: Python archive SHA-256 mismatch (possible tamper / wrong build)."
  echo "         got:      $GOT_SHA"
  echo "         expected: $PYTHON_SHA256"
  exit 1
fi
echo "  [2/4] Extracting (sha256 verified)..."
tar -xzf "$TMPDIR/python.tar.gz" -C "$AIDOCS_HOME"
mv "$AIDOCS_HOME/python/install" "$AIDOCS_HOME/python-tmp" 2>/dev/null || true
if [ -d "$AIDOCS_HOME/python-tmp" ]; then
  rm -rf "$AIDOCS_HOME/python"
  mv "$AIDOCS_HOME/python-tmp" "$AIDOCS_HOME/python"
fi

PYTHON="$AIDOCS_HOME/python/bin/python3"
if [ ! -f "$PYTHON" ]; then
  # Some builds put it directly in python/
  PYTHON="$AIDOCS_HOME/python/bin/python3.12"
fi

if [ ! -f "$PYTHON" ]; then
  echo "  ERROR: Python binary not found after extraction"
  ls -la "$AIDOCS_HOME/python/" 2>/dev/null
  ls -la "$AIDOCS_HOME/python/bin/" 2>/dev/null
  exit 1
fi

echo "  Python: $("$PYTHON" --version 2>&1)"

# ── Install aidocs-mcp ──
echo "  [3/4] Installing aidocs-mcp..."
"$PYTHON" -m pip install --upgrade pip -q --root-user-action=ignore 2>/dev/null || true
# `pipe | tail` masks pip's exit code; PIPESTATUS preserves it so a missing
# network or rate-limited PyPI surfaces as an installer failure instead of a
# confusing "MCP server failed" error in the verification step.
pip_log="$(mktemp)"
if ! "$PYTHON" -m pip install aidocs-mcp -q --root-user-action=ignore >"$pip_log" 2>&1; then
  echo "  ERROR: Failed to install aidocs-mcp"
  tail -10 "$pip_log" | sed 's/^/    /'
  echo
  echo "  Common causes: no network, TLS certificate issue, or PyPI rate limit."
  echo "  Retry: \"$PYTHON\" -m pip install aidocs-mcp"
  rm -f "$pip_log"
  exit 1
fi
tail -3 "$pip_log"
rm -f "$pip_log"

# ── Create bin wrapper ──
mkdir -p "$AIDOCS_HOME/bin"
cat > "$AIDOCS_HOME/bin/aidocs" << WRAPPER
#!/usr/bin/env bash
exec "$PYTHON" -m aidocs_mcp.cli "\$@"
WRAPPER
chmod +x "$AIDOCS_HOME/bin/aidocs"

# ── Add to PATH ──
SHELL_RC=""
if [ -f "$HOME/.bashrc" ]; then SHELL_RC="$HOME/.bashrc"; fi
if [ -f "$HOME/.zshrc" ]; then SHELL_RC="$HOME/.zshrc"; fi

PATH_LINE='export PATH="$HOME/.aidocs/bin:$PATH"'
if [ -n "$SHELL_RC" ]; then
  if ! grep -q ".aidocs/bin" "$SHELL_RC" 2>/dev/null; then
    echo "" >> "$SHELL_RC"
    echo "# AIDOCS" >> "$SHELL_RC"
    echo "$PATH_LINE" >> "$SHELL_RC"
    echo "  Added to PATH in $(basename $SHELL_RC)"
  fi
fi

export PATH="$AIDOCS_HOME/bin:$PATH"

# ── Verify ──
echo "  [4/4] Verifying..."
AIDOCS_VERSION="$("$AIDOCS_HOME/bin/aidocs" version 2>&1 || echo 'FAILED')"
echo "  aidocs: $AIDOCS_VERSION"

echo ""
echo "  ════════════════════════════════════════"
echo "  Installation complete!"
echo "  ════════════════════════════════════════"
echo ""
echo "  Next steps:"
echo "    1. Restart your shell so PATH is refreshed (or run: $PATH_LINE)"
echo "    2. aidocs setup /path/to/your/project"
echo "    3. aidocs doctor    (verify the install)"
echo "    4. Open the project in your IDE and type /aidocs"
echo ""
echo "  If 'aidocs' is not found after restart, run the bin wrapper directly:"
echo "    $AIDOCS_HOME/bin/aidocs"
echo ""
