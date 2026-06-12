# dvx

Claude Code orchestrator that automates implement/review/test/commit cycles.

## Install

Paste this prompt into Claude Code in your project:

```
Install dvx for me:

1. Run: curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
2. Download the goal template into this project:
   curl -s https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/GOAL.md.example -o GOAL.md.example
3. Make sure ~/.dvx/bin is on my PATH. If it isn't, add this to my shell config:
   export PATH="${HOME}/.dvx/bin:$PATH"
```

## Start the watcher

In a separate terminal, in your project directory:

```bash
dvx watch
```

It watches `.dvx/goals/` for `GOAL-*.md` files; each goal gets its own branch, a headless Claude session implements it, and changes are committed in logical groups and merged back. A killed watcher resumes where it left off (`dvx clear` resets watcher state). The watcher only claims goals when the working tree is clean — commit or stash first; blocked goals are reported along with the dirty paths.

## Queue a goal

Paste this prompt into Claude Code:

```
Read GOAL.md.example, then create a well-scoped goal file in .dvx/goals/ for this task:

<describe your task>

The filename becomes the branch name (GOAL-my-change.md → branch goal-my-change).
The goal file is the entire instruction set the implementer receives, so make it
self-contained.
```

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated

## Manual installation

`install.sh` auto-detects how it's run: piped from curl it downloads the repo and installs; run from a local clone it installs from the checkout.

```bash
git clone https://github.com/dvx-sh/dvx-cli.git
cd dvx-cli
./install.sh
```

Flags: `--local`, `--remote`, `--dev`; `./install.sh --help` shows usage. Re-run the installer to upgrade.

## Beyond watch

dvx also includes a plan-based orchestrator: `dvx plan` generates a plan and `dvx run` drives implement/review/test/commit cycles over it. Installing dvx adds `/dvx:*` skills to Claude Code (`/dvx:help` lists them). Run `dvx --help` or ask Claude Code in the cloned repo for details.

## License

Apache 2.0
