#!/bin/bash
# Install dvx from GitHub
# Usage: curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install-remote.sh | bash

set -e

DVX_HOME="${HOME}/.dvx"
REPO_URL="https://github.com/dvx-sh/dvx-cli/archive/refs/heads/main.tar.gz"
TMP_DIR=$(mktemp -d)

cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "Installing dvx to ${DVX_HOME}..."

# Download and extract
echo "Downloading..."
curl -fsSL "$REPO_URL" | tar -xz -C "$TMP_DIR"

# Copy files
mkdir -p "$DVX_HOME"
cp -r "$TMP_DIR/dvx-cli-main/dvx/"* "$DVX_HOME/"

# Make scripts executable
chmod +x "$DVX_HOME/bin/"*

# Run setup
"$DVX_HOME/bin/setup"
