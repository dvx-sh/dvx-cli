#!/bin/bash
# Install dvx from this repo to ~/.dvx

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DVX_HOME="${HOME}/.dvx"
CLAUDE_COMMANDS="${HOME}/.claude/commands/dvx"

echo "Installing dvx to ${DVX_HOME}..."

# Copy files
mkdir -p "$DVX_HOME"
cp -r "$SCRIPT_DIR/dvx/"* "$DVX_HOME/"

# Make scripts executable
chmod +x "$DVX_HOME/bin/"*

# Install skills to Claude Code commands directory
echo "Installing skills to ${CLAUDE_COMMANDS}..."
mkdir -p "$CLAUDE_COMMANDS"
for skill in "$DVX_HOME/src/skills/"*.md; do
    name=$(basename "$skill")
    # Skip template files
    if [[ "$name" != _* ]]; then
        cp "$skill" "$CLAUDE_COMMANDS/"
    fi
done

# Run setup
"$DVX_HOME/bin/setup"
