# dvx

Agent orchestrator that automates implement/review/test/commit cycles through Claude Code or Codex.

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

It watches `.dvx/todo/` for work files. `GOAL*.md` files keep the Claude `/goal` flow for Claude models and use a Codex exec prompt for GPT models; any other regular file (`PLAN*.md`, `TASK*.md`, `TODO*.md`, `.txt`, etc.) runs through the same loop as `dvx run`. Outside of control files (`MERGE`, `SYNC`, `STOP`), files are processed oldest-first by file modification time, then filename. Each file gets its own branch, a headless agent session implements it with the configured model (default: `claude-opus-4-8`), and changes are committed in logical groups and merged back. A killed watcher resumes where it left off (`dvx clear` resets watcher state). The watcher only claims files when the working tree is clean — commit or stash first; blocked files are reported along with the dirty paths. Merges land on the branch the watcher was started on, which is then pushed to its remote (when one exists) — run the watcher on a dedicated branch and review the work there; nothing touches main unless you start the watcher on main.


### Agent model selection

`dvx` defaults to `claude-opus-4-8`. Override it per command with `--model`, or for all commands with `DVX_MODEL`:

```bash
DVX_MODEL=claude-sonnet-4-6 dvx run PLAN-example.md
dvx run --model claude-opus-4-8 PLAN-example.md
dvx watch --model gpt-5.5
```

`dvx watch` and non-blocked `dvx run` route `gpt-*` models through `codex exec` with no approval prompts and `model_reasoning_effort="xhigh"`. Other selected models use Claude Code with `--effort high`. `dvx plan`, `dvx interview`, `dvx autopilot`, and saved `dvx run` blocked-state recovery remain Claude-only for now and reject `gpt-*` before launching Claude.

## Queue work

Paste this prompt into Claude Code:

```
Read GOAL.md.example, then create a well-scoped GOAL*.md file in .dvx/todo/ for this task:

<describe your task>

The filename becomes the branch name (GOAL-my-change.md → branch goal-my-change).
The goal file is the entire instruction set the implementer receives, so make it
self-contained.
```

You can also drop non-GOAL work files into `.dvx/todo/`; those run through the
same loop as `dvx run`.

## Merge the watch branch

Drop a file named `MERGE` in `.dvx/todo/` to ask the watcher to merge the watch branch into a remote branch. An empty file targets the remote's default branch; otherwise the file contains a single branch name and nothing else (e.g. `dev` — not `origin/dev`, no prose) — agents generate this file, so the convention matters.

```bash
touch .dvx/todo/MERGE            # merge into the default branch
echo dev > .dvx/todo/MERGE       # merge into origin/dev
```

The merge runs between queued files — after the in-flight item finishes (if any) and before the next queued one; it takes precedence over the queue. The watcher fetches, merges the remote target into the watch branch (the selected agent resolves any conflicts), fast-forwards the remote target to the watch branch tip — never force-pushed, so if the target advances mid-merge the watcher re-fetches and re-merges instead of clobbering it — then pushes the watch branch. The MERGE file is consumed when claimed. Like queued files, the merge only starts on a clean working tree, and it requires a git remote with the target branch already on it.

## Sync the watch branch

Drop a file named `SYNC` in `.dvx/todo/` to ask the watcher to merge a remote branch into the watch branch and push the watch branch. An empty file syncs from the remote's default branch; otherwise the file contains a single branch name and nothing else.

```bash
touch .dvx/todo/SYNC            # sync from the default branch
echo dev > .dvx/todo/SYNC       # sync from origin/dev
```

SYNC runs between queued files, after any MERGE request and before normal queued work. If pushing the watch branch is rejected because `origin/<watch-branch>` advanced, the watcher fetches and merges that remote watch branch, re-fetches and re-merges the sync source, then retries the push without force-pushing. A dirty tree blocks SYNC and leaves the SYNC file in place; deleting that file clears the control block on the next watch pass.

## Stop the watcher cleanly

Drop a file named `STOP` in `.dvx/todo/` to ask the watcher to exit with status 0 after the active item, MERGE, or SYNC finishes. STOP is content-agnostic and stateless: the file is consumed when noticed. If the watcher is idle or blocked, STOP exits immediately without clearing preserved watch state.

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated for Claude models
- Codex CLI installed and authenticated for `gpt-*` models

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
