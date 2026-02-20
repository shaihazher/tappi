#!/usr/bin/env bash
# tappi launcher for Linux — double-click to start
# Activates venv, launches browser, starts web UI, opens in browser
set -euo pipefail

VENV_DIR="$HOME/.tappi-venv"

if [ ! -d "$VENV_DIR" ]; then
  echo "❌ tappi not installed. Run the installer first:"
  echo "   curl -fsSL https://raw.githubusercontent.com/shaihazher/tappi/main/install/install-linux.sh | bash"
  echo ""
  read -p "Press Enter to close..."
  exit 1
fi

source "$VENV_DIR/bin/activate"

echo "🚀 Starting tappi..."
echo ""

# Launch browser in background
bpy launch &>/dev/null &
sleep 2

# Start web UI in background
bpy serve &
SERVER_PID=$!
sleep 2

# Open in default browser
xdg-open "http://127.0.0.1:8321" 2>/dev/null || sensible-browser "http://127.0.0.1:8321" 2>/dev/null || echo "Open http://127.0.0.1:8321 in your browser"

echo "✅ tappi is running at http://127.0.0.1:8321"
echo "   Close this window to stop the server."
echo ""

# Wait for server (keeps terminal open)
wait $SERVER_PID
