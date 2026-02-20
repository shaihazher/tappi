#!/usr/bin/env bash
# tappi installer for macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/shaihazher/tappi/main/install/install-macos.sh | bash
set -euo pipefail

VENV_DIR="$HOME/.tappi-venv"
MIN_PY="3.10"

echo "🐍 tappi installer for macOS"
echo "──────────────────────────────"

# --- Helpers ---
version_gte() {
  printf '%s\n%s' "$1" "$2" | sort -V | head -n1 | grep -qx "$2"
}

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
      ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
      if [ -n "$ver" ] && version_gte "$ver" "$MIN_PY"; then
        echo "$cmd"
        return 0
      fi
    fi
  done
  return 1
}

# --- Step 1: Ensure Python ---
if PYTHON=$(find_python); then
  echo "✓ Found $($PYTHON --version)"
else
  echo "⚠ Python $MIN_PY+ not found. Installing via Homebrew..."
  if ! command -v brew &>/dev/null; then
    echo "  Installing Homebrew first..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
  fi
  brew install python@3.13
  PYTHON=$(find_python) || { echo "❌ Python installation failed"; exit 1; }
  echo "✓ Installed $($PYTHON --version)"
fi

# --- Step 2: Create venv ---
echo "📦 Creating virtual environment at $VENV_DIR..."
"$PYTHON" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# --- Step 3: Upgrade pip & install tappi ---
echo "⬆️  Upgrading pip..."
pip install --upgrade pip -q
echo "📥 Installing tappi..."
pip install tappi

# --- Step 4: Shell integration ---
ACTIVATE_LINE="source $VENV_DIR/bin/activate"
SHELL_NAME=$(basename "$SHELL")
RC_FILE="$HOME/.zshrc"
[ "$SHELL_NAME" = "bash" ] && RC_FILE="$HOME/.bash_profile"

if ! grep -qF "$ACTIVATE_LINE" "$RC_FILE" 2>/dev/null; then
  echo "" >> "$RC_FILE"
  echo "# tappi virtual environment" >> "$RC_FILE"
  echo "$ACTIVATE_LINE" >> "$RC_FILE"
  echo "✓ Added activation to $RC_FILE"
fi

# --- Step 5: Create desktop launcher ---
LAUNCHER_URL="https://raw.githubusercontent.com/shaihazher/tappi/main/install/launch-macos.command"
LAUNCHER_PATH="$HOME/Desktop/Launch tappi.command"
curl -fsSL "$LAUNCHER_URL" -o "$LAUNCHER_PATH"
chmod +x "$LAUNCHER_PATH"
echo "✓ Created 'Launch tappi' on Desktop"

echo ""
echo "──────────────────────────────"
echo "✅ tappi installed!"
echo ""
echo "   Double-click 'Launch tappi' on your Desktop to start."
echo "   Pick your AI provider in the Settings page that opens."
