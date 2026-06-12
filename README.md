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

It watches `.dvx/goals/` for `GOAL-*.md` files; each goal gets its own branch, a headless Claude session implements it, and changes are committed in logical groups and merged back. A killed watcher resumes where it left off (`dvx clear` resets watcher state). The watcher only claims goals when the working tree is clean — commit or stash first; blocked goals are reported along with the dirty paths. Merges land on the branch the watcher was started on, which is then pushed to its remote (when one exists) — run the watcher on a dedicated branch and review the work there; nothing touches main unless you start the watcher on main. Each goal file specifies verification commands the implementer must pass before it can finish.

## Queue a goal

Paste this prompt into Claude Code:

```
Read GOAL.md.example, then create a well-scoped goal file in .dvx/goals/ for this task:

<describe your task>

The filename becomes the branch name (GOAL-my-change.md → branch goal-my-change).
The goal file is the entire instruction set the implementer receives, so make it
self-contained.
```

## Merge the watch branch

Drop a file named `MERGE` in `.dvx/goals/` to ask the watcher to merge the watch branch into a remote branch. An empty file targets the remote's default branch; otherwise the file contains a single branch name and nothing else (e.g. `dev` — not `origin/dev`, no prose) — agents generate this file, so the convention matters.

```bash
touch .dvx/goals/MERGE            # merge into the default branch
echo dev > .dvx/goals/MERGE       # merge into origin/dev
```

The merge runs between goals — after the in-flight goal finishes (if any) and before the next queued one; it takes precedence over the queue. The watcher fetches, merges the remote target into the watch branch (a Claude session resolves any conflicts), fast-forwards the remote target to the watch branch tip — never force-pushed, so if the target advances mid-merge the watcher re-fetches and re-merges instead of clobbering it — then pushes the watch branch. The MERGE file is consumed when claimed. Like goals, the merge only starts on a clean working tree, and it requires a git remote with the target branch already on it.

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated

## Manual installation

`install.sh` auto-detects how it's run: piped from curl it downloads the repo and installs; run from a local clone it installs from the checkout. (It sits at the repo root, so you can read exactly what the curl pipes to bash before running it.)

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
