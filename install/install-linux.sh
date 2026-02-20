#!/usr/bin/env bash
# tappi installer for Linux (Debian/Ubuntu, Fedora/RHEL, Arch)
# Usage: curl -fsSL https://raw.githubusercontent.com/shaihazher/tappi/main/install/install-linux.sh | bash
set -euo pipefail

VENV_DIR="$HOME/.tappi-venv"
MIN_PY="3.10"

echo "🐍 tappi installer for Linux"
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

detect_pkg_manager() {
  if command -v apt-get &>/dev/null; then echo "apt"
  elif command -v dnf &>/dev/null; then echo "dnf"
  elif command -v pacman &>/dev/null; then echo "pacman"
  else echo "unknown"
  fi
}

install_python() {
  local pm
  pm=$(detect_pkg_manager)
  echo "⚠ Python $MIN_PY+ not found. Installing..."
  case "$pm" in
    apt)
      sudo apt-get update -qq
      sudo apt-get install -y python3 python3-pip python3-venv
      ;;
    dnf)
      sudo dnf install -y python3 python3-pip
      ;;
    pacman)
      sudo pacman -Sy --noconfirm python python-pip
      ;;
    *)
      echo "❌ Unsupported package manager. Install Python $MIN_PY+ manually, then re-run."
      exit 1
      ;;
  esac
}

# --- Step 1: Ensure Python ---
if PYTHON=$(find_python); then
  echo "✓ Found $($PYTHON --version)"
else
  install_python
  PYTHON=$(find_python) || { echo "❌ Python installation failed"; exit 1; }
  echo "✓ Installed $($PYTHON --version)"
fi

# --- Step 2: Ensure venv module ---
if ! "$PYTHON" -m venv --help &>/dev/null; then
  echo "📦 Installing venv module..."
  pm=$(detect_pkg_manager)
  PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  case "$pm" in
    apt) sudo apt-get install -y "python${PY_VER}-venv" ;;
    dnf) ;; # venv included in python3 on Fedora
    pacman) ;; # venv included in python on Arch
  esac
fi

# --- Step 3: Create venv ---
echo "📦 Creating virtual environment at $VENV_DIR..."
"$PYTHON" -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

# --- Step 4: Upgrade pip & install tappi ---
echo "⬆️  Upgrading pip..."
pip install --upgrade pip -q
echo "📥 Installing tappi..."
pip install tappi

# --- Step 5: Shell integration ---
ACTIVATE_LINE="source $VENV_DIR/bin/activate"
SHELL_NAME=$(basename "$SHELL")
RC_FILE="$HOME/.bashrc"
[ "$SHELL_NAME" = "zsh" ] && RC_FILE="$HOME/.zshrc"
[ "$SHELL_NAME" = "fish" ] && RC_FILE="$HOME/.config/fish/config.fish" && ACTIVATE_LINE="source $VENV_DIR/bin/activate.fish"

if ! grep -qF "$ACTIVATE_LINE" "$RC_FILE" 2>/dev/null; then
  echo "" >> "$RC_FILE"
  echo "# tappi virtual environment" >> "$RC_FILE"
  echo "$ACTIVATE_LINE" >> "$RC_FILE"
  echo "✓ Added activation to $RC_FILE"
fi

# --- Step 6: Create desktop launcher ---
LAUNCHER_URL="https://raw.githubusercontent.com/shaihazher/tappi/main/install/launch-linux.sh"
DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
LAUNCHER_PATH="$DESKTOP_DIR/Launch tappi.sh"
mkdir -p "$DESKTOP_DIR"
curl -fsSL "$LAUNCHER_URL" -o "$LAUNCHER_PATH"
chmod +x "$LAUNCHER_PATH"
echo "✓ Created 'Launch tappi' on Desktop"

echo ""
echo "──────────────────────────────"
echo "✅ tappi installed!"
echo ""
echo "   Double-click 'Launch tappi' on your Desktop to start."
echo "   Pick your AI provider in the Settings page that opens."
