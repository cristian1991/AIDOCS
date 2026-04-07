#!/usr/bin/env bash
# AIDOCS Universal Installer — zero dependencies, one command
# Usage: curl -fsSL https://raw.githubusercontent.com/cristian1991/AIDOCS/main/core/scripts/install.sh | bash
set -euo pipefail

AIDOCS_HOME="${AIDOCS_HOME:-$HOME/.aidocs}"
PYTHON_VERSION="3.12.10"
PYTHON_BUILD_TAG="20250409"

# ── Detect platform ──
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64)  PLATFORM="x86_64-unknown-linux-gnu" ;;
      aarch64) PLATFORM="aarch64-unknown-linux-gnu" ;;
      *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    ;;
  Darwin)
    case "$ARCH" in
      x86_64)  PLATFORM="x86_64-apple-darwin" ;;
      arm64)   PLATFORM="aarch64-apple-darwin" ;;
      *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
    esac
    ;;
  *) echo "Unsupported OS: $OS (use the Windows installer instead)"; exit 1 ;;
esac

PYTHON_URL="https://github.com/indygreg/python-build-standalone/releases/download/${PYTHON_BUILD_TAG}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_TAG}-${PLATFORM}-install_only.tar.gz"

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
  read -p "  Reinstall? [y/N] " -n 1 -r
  echo
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
echo "  [2/4] Extracting..."
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
"$PYTHON" -m pip install aidocs-mcp -q --root-user-action=ignore 2>&1 | tail -3

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
echo "  Run: aidocs setup /path/to/your/project"
echo ""
echo "  If 'aidocs' is not found, restart your terminal or run:"
echo "  $PATH_LINE"
echo ""
