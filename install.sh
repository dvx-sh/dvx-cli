#!/bin/bash
# Install dvx to ~/.dvx
#
# Works two ways and auto-detects which:
#   1. Local clone:   ./install.sh           (installs from this checkout)
#   2. Remote (curl): curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
#                                             (downloads the repo, then installs)
#
# Pass --dev to also install dev dependencies (pytest, ruff).

set -e

DVX_HOME="${HOME}/.dvx"
CLAUDE_COMMANDS="${HOME}/.claude/commands/dvx"
DVX_BRANCH="${DVX_BRANCH:-main}"
REPO_TARBALL="https://github.com/dvx-sh/dvx-cli/archive/refs/heads/${DVX_BRANCH}.tar.gz"

TMP_DIR=""
cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

# --- Detect install source ---------------------------------------------------
# When run from a local clone, this script is a real file whose directory
# contains the dvx/ payload. When piped from curl, $BASH_SOURCE is "bash"
# (not a file), so we fall back to downloading the repo.
SOURCE="${BASH_SOURCE[0]:-$0}"
if [ -f "$SOURCE" ] && [ -d "$(cd "$(dirname "$SOURCE")" && pwd)/dvx" ]; then
    SRC_ROOT="$(cd "$(dirname "$SOURCE")" && pwd)"
    echo "Installing dvx from local clone: ${SRC_ROOT}"
else
    echo "Installing dvx from GitHub (${DVX_BRANCH})..."
    for tool in curl tar; do
        if ! command -v "$tool" &> /dev/null; then
            echo "Error: '$tool' is required for remote install but was not found." >&2
            exit 1
        fi
    done
    TMP_DIR=$(mktemp -d)
    echo "Downloading..."
    curl -fsSL "$REPO_TARBALL" | tar -xz -C "$TMP_DIR"
    SRC_ROOT="${TMP_DIR}/dvx-cli-${DVX_BRANCH}"
fi

DVX_PAYLOAD="${SRC_ROOT}/dvx"
if [ ! -d "$DVX_PAYLOAD" ]; then
    echo "Error: could not find dvx payload at ${DVX_PAYLOAD}" >&2
    exit 1
fi

# --- Copy files --------------------------------------------------------------
echo "Installing dvx to ${DVX_HOME}..."
mkdir -p "$DVX_HOME"
cp -r "$DVX_PAYLOAD/"* "$DVX_HOME/"

# Make scripts executable
chmod +x "$DVX_HOME/bin/"*

# --- Install Claude Code skills ----------------------------------------------
echo "Installing skills to ${CLAUDE_COMMANDS}..."
mkdir -p "$CLAUDE_COMMANDS"
for skill in "$DVX_HOME/src/skills/"*.md; do
    name=$(basename "$skill")
    # Skip template files (leading underscore)
    if [[ "$name" != _* ]]; then
        cp "$skill" "$CLAUDE_COMMANDS/"
    fi
done

# --- Run setup ---------------------------------------------------------------
"$DVX_HOME/bin/setup" "$@"
