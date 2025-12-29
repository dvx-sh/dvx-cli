#!/bin/bash
# Install dvx from this repo to ~/.dvx

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DVX_HOME="${HOME}/.dvx"

echo "Installing dvx to ${DVX_HOME}..."

# Copy files
mkdir -p "$DVX_HOME"
cp -r "$SCRIPT_DIR/dvx/"* "$DVX_HOME/"

# Make scripts executable
chmod +x "$DVX_HOME/bin/"*

# Run setup
"$DVX_HOME/bin/setup"
