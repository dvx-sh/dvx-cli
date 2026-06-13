#!/bin/bash
# Install dvx to ~/.dvx
#
# Works two ways and auto-detects which:
#   1. Local clone:   ./install.sh           (installs from this checkout)
#   2. Remote (curl): curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
#                                             (downloads the repo, then installs)
#
# Flags:
#   --local     Require a local checkout (this script next to pyproject.toml);
#               errors out instead of falling back to download.
#   --remote    Always download the repo tarball and install from it, even
#               when run from a checkout.
#   --dev       Forwarded to ~/.dvx/bin/setup to also install dev
#               dependencies (pytest, ruff).
#   -h, --help  Print usage and exit.
#
# Environment:
#   DVX_BRANCH        Branch to download in remote mode (default: main).
#   DVX_REPO_TARBALL  Tarball URL override for remote mode (default: the
#                     GitHub archive for DVX_BRANCH).

set -e

DVX_HOME="${HOME}/.dvx"
CLAUDE_COMMANDS="${HOME}/.claude/commands/dvx"
DVX_BRANCH="${DVX_BRANCH:-main}"
REPO_TARBALL="${DVX_REPO_TARBALL:-https://github.com/dvx-sh/dvx-cli/archive/refs/heads/${DVX_BRANCH}.tar.gz}"

usage() {
    cat <<EOF
Usage: install.sh [--local | --remote] [--dev]

Installs dvx to ~/.dvx and its skills to ~/.claude/commands/dvx.

Modes (default: auto-detect):
  --local     Install from the checkout containing this script. Errors if
              the script is not next to the project root; never downloads.
  --remote    Download the repo tarball and install from it, even when run
              from a checkout.
  (no flag)   Use the local checkout when available, otherwise download.

Options:
  --dev       Forwarded to ~/.dvx/bin/setup to install dev dependencies
              (pytest, ruff).
  -h, --help  Show this help and exit.
EOF
}

MODE="auto"
SETUP_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --local)
            if [ "$MODE" = "remote" ]; then
                echo "Error: --local and --remote are mutually exclusive." >&2
                usage >&2
                exit 1
            fi
            MODE="local"
            ;;
        --remote)
            if [ "$MODE" = "local" ]; then
                echo "Error: --local and --remote are mutually exclusive." >&2
                usage >&2
                exit 1
            fi
            MODE="remote"
            ;;
        --dev)
            SETUP_ARGS+=("--dev")
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown option '$1'" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

TMP_DIR=""
cleanup() {
    if [ -n "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}
trap cleanup EXIT

# --- Detect install source ---------------------------------------------------
# When run from a local clone, this script is a real file whose directory
# contains the project root. When piped from curl, $BASH_SOURCE is "bash"
# (not a file), so we fall back to downloading the repo.
SOURCE="${BASH_SOURCE[0]:-$0}"
LOCAL_ROOT=""
if [ -f "$SOURCE" ]; then
    SOURCE_DIR="$(cd "$(dirname "$SOURCE")" && pwd)"
    if [ -f "$SOURCE_DIR/pyproject.toml" ]; then
        LOCAL_ROOT="$SOURCE_DIR"
    fi
fi

if [ "$MODE" = "local" ] && [ -z "$LOCAL_ROOT" ]; then
    echo "Error: --local requires running install.sh from a checkout root containing pyproject.toml." >&2
    exit 1
fi
if [ "$MODE" = "auto" ]; then
    if [ -n "$LOCAL_ROOT" ]; then
        MODE="local"
    else
        MODE="remote"
    fi
fi

if [ "$MODE" = "local" ]; then
    SRC_ROOT="$LOCAL_ROOT"
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

DVX_PAYLOAD="$SRC_ROOT"
if [ ! -f "$DVX_PAYLOAD/pyproject.toml" ] || [ ! -d "$DVX_PAYLOAD/src" ] || [ ! -d "$DVX_PAYLOAD/bin" ]; then
    echo "Error: could not find dvx project payload at ${DVX_PAYLOAD}" >&2
    exit 1
fi

# --- Copy files --------------------------------------------------------------
echo "Installing dvx to ${DVX_HOME}..."
mkdir -p "$DVX_HOME"
# Remove installed package directories first so files deleted from the package
# don't linger across upgrades. The venv lives at ${DVX_HOME}/.venv and is
# untouched.
for dir in bin src tests plans; do
    rm -rf "$DVX_HOME/$dir"
    if [ -d "$DVX_PAYLOAD/$dir" ]; then
        cp -r "$DVX_PAYLOAD/$dir" "$DVX_HOME/"
    fi
done

for file in pyproject.toml tasks.py uv.lock; do
    if [ -f "$DVX_PAYLOAD/$file" ]; then
        cp "$DVX_PAYLOAD/$file" "$DVX_HOME/"
    fi
done

# Copy the todo template so AI agents can reference it without curl
cp "$DVX_PAYLOAD/TODO.md.example" "$DVX_HOME/TODO.md.example"

# Make scripts executable
chmod +x "$DVX_HOME/bin/"*

# --- Install Claude Code skills ----------------------------------------------
echo "Installing skills to ${CLAUDE_COMMANDS}..."
mkdir -p "$CLAUDE_COMMANDS"
# The installer owns this directory: clear it first so skills deleted from
# the package stop being /dvx:* commands.
rm -f "$CLAUDE_COMMANDS"/*.md
for skill in "$DVX_HOME/src/skills/"*.md; do
    name=$(basename "$skill")
    # Skip template files (leading underscore)
    if [[ "$name" != _* ]]; then
        cp "$skill" "$CLAUDE_COMMANDS/"
    fi
done

# --- Run setup ---------------------------------------------------------------
"$DVX_HOME/bin/setup" "${SETUP_ARGS[@]}"
