#!/bin/bash
# Bundle Python virtual environment for the Electron app
# Run from the project root: bash desktop/scripts/bundle-python.sh

set -e

echo "=== Bundling Python environment for Videoscriber ==="

# Create a portable venv in the project root (Electron will bundle it)
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies..."
source .venv/bin/activate
pip install -r requirements.txt --quiet

echo "=== Python environment bundled ==="
echo "The .venv directory will be included in the Electron app package."
echo ""
echo "To build the desktop app:"
echo "  cd desktop && npm install && npm run make"
