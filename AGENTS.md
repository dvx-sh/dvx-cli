# dvx-cli

## Project Summary

`dvx` is a Python CLI that orchestrates a Claude Code driven development loop:

1. Generate or update a plan file with `dvx plan`
2. Run implementation/review/test/commit cycles with `dvx run`
3. Track orchestration state in a local `.dvx/` directory
4. Preserve per-plan decisions and blocked context for later resumption

The CLI does not execute tasks directly. It loads markdown skill files from `dvx/src/skills/` and sends those prompts to Claude Code through the wrappers in `dvx/src/claude_session.py`.

## Repository Layout

- `dvx/src/cli.py`: command-line entrypoint for `plan`, `run`, `status`, `decisions`, and `clean`
- `dvx/src/orchestrator.py`: main implement -> review -> test -> commit loop and finalization flow
- `dvx/src/plan_parser.py`: Claude-assisted task extraction and `.dvx/task-status.json` tracking
- `dvx/src/state.py`: persistent per-plan state under `.dvx/<plan-name>/`
- `dvx/src/claude_session.py`: wrappers around the `claude` CLI for interactive and non-interactive sessions
- `dvx/src/skills/*.md`: prompt templates used both internally and as `/dvx:*` Claude Code commands
- `dvx/bin/dvx`: installed launcher that executes `~/.dvx/src/cli.py` with `~/.dvx/.venv/bin/python`
- `dvx/bin/setup`: creates the installed virtualenv and runs `pip install -e .`
- `dvx/bin/dev-setup`: local repo development bootstrap
- `dvx/tests/`: pytest coverage for CLI, parser, state, orchestrator, and Claude session behavior

## Runtime and Dependencies

- Python requirement: 3.10+
- Core dependency: `pyyaml`
- Optional extras:
  - `.[dev]`: `pytest`, `ruff`, `invoke`
  - `.[automation]`: `invoke`, `fabric`
  - `.[ai]`: `anthropic`
- External requirement: Claude Code CLI must already be installed and authenticated

## Installation Scripts

### `install.sh`

Local install from a checked out repository:

1. Copies `dvx/` into `~/.dvx/`
2. Makes `~/.dvx/bin/*` executable
3. Copies skill markdown files into `~/.claude/commands/dvx/` except template files starting with `_`
4. Runs `~/.dvx/bin/setup`

Use this when working from a local clone.

### `install-remote.sh`

Remote install from GitHub:

1. Downloads `main.tar.gz` from `https://github.com/dvx-sh/dvx-cli`
2. Extracts it into a temporary directory
3. Copies `dvx/` into `~/.dvx/`
4. Makes `~/.dvx/bin/*` executable
5. Runs `~/.dvx/bin/setup`

Unlike `install.sh`, this script does not separately copy skills into `~/.claude/commands/dvx/`. Skills are still installed on CLI startup by `ensure_skills_installed()` in `dvx/src/cli.py`.

### `dvx/bin/setup`

Installed-environment bootstrap:

1. Verifies `python3` exists
2. Creates `~/.dvx/.venv` if needed
3. Upgrades `pip`
4. Runs `pip install -e .` inside `~/.dvx`
5. Optionally installs `.[dev]` when invoked with `--dev`

This script prints the expected shell PATH update:

```bash
export PATH="${HOME}/.dvx/bin:$PATH"
```

### `dvx/bin/dev-setup`

Repository-local development bootstrap:

1. Creates `.venv/` in the repo root
2. Installs `pip install -e ".[dev]"`
3. Leaves the repo ready for local lint/test work

## Development Commands

From the repo root:

```bash
dvx/bin/dev-setup
source .venv/bin/activate
invoke tests
invoke lint
invoke check
```

## Installed Command Behavior

After installation, `dvx` resolves to `dvx/bin/dvx`, which:

1. Checks for `~/.dvx/.venv/bin/python`
2. Errors if setup has not been run
3. Executes `~/.dvx/src/cli.py` with the installed virtualenv

## State and Working Files

When `dvx run` operates on a plan file, it stores working state in `.dvx/` inside the target project:

- `.dvx/<plan-name>/state.json`
- `.dvx/<plan-name>/blocked-context.md`
- `.dvx/<plan-name>/dvx.log`
- `.dvx/<plan-name>/DECISIONS-*.md`
- `.dvx/task-status.json`

The per-plan directory name is based on the plan file basename, not its full path.
