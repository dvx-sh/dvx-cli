# dvx

Claude Code orchestrator that automates implement/review/test/commit cycles.

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated

## Installation

`install.sh` auto-detects how it's run: piped from curl it downloads the repo and installs; run from a local clone it installs from the checkout.

```bash
curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install.sh | bash
```

Or clone and run the same script locally:

```bash
git clone https://github.com/dvx-sh/dvx-cli.git
cd dvx-cli
./install.sh           # add --dev to also install pytest, ruff, invoke
```

Flags override auto-detection: `--local` installs from the checkout and never downloads, `--remote` always downloads the repo even from a checkout, and `--dev` also installs dev dependencies. `./install.sh --help` shows full usage.

Re-running the installer replaces `~/.dvx/src` and reinstalls the Claude Code skills, so files and skills removed upstream are pruned on upgrade.

Add to your shell config (~/.bashrc, ~/.zshrc, etc.):

```bash
export PATH="${HOME}/.dvx/bin:$PATH"
```

### Optional dependencies

```bash
cd ~/.dvx && source .venv/bin/activate
pip install -e ".[dev]"        # pytest, ruff, invoke
pip install -e ".[automation]" # invoke, fabric
pip install -e ".[ai]"         # anthropic
```

## Usage

```bash
# Generate a plan with Claude
echo "Create a user authentication system" | dvx plan PLAN-auth.md
dvx plan                       # Opens editor, Claude names the file
dvx plan --consensus           # Planner/Architect/Critic consensus loop

# Run orchestration
dvx run PLAN-feature.md        # Run orchestration
dvx run -s PLAN-feature.md     # Step mode: pause after each task
dvx run -f PLAN-feature.md     # Force restart with new plan

dvx status PLAN-feature.md     # Show current status for a plan
dvx decisions PLAN-feature.md  # Show decisions made for a plan
dvx clean [PLAN-feature.md]    # Delete plan state (all of .dvx/ if omitted)

# Higher-level workflows
dvx interview "task"           # Deep-interview session producing an execution-ready spec
dvx autopilot "task"           # Sequence interview → consensus plan → run end-to-end
```

The `run` command handles everything automatically:
- **No state**: Starts fresh orchestration
- **Blocked**: Launches interactive Claude session to resolve, then continues
- **Paused** (step mode): Continues to next task
- **Complete**: Shows completion message

## Goal Watch

`dvx watch` watches a goals directory (default `.dvx/goals`) for `GOAL-*.md` files and processes them one at a time: each goal gets its own branch, a headless Claude session implements it, changes are committed in logical groups, and the branch is merged back. State persists in `.dvx/watch/`, so a killed watcher resumes where it left off. `dvx clear` resets goal-processing state (the goals directory itself is untouched). See `dvx watch --help` for options.

```bash
dvx watch
```

Queue a goal from the template:

```bash
curl -s https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/GOAL.md.example -o .dvx/goals/GOAL-my-change.md
```

Fill in the template's sections — the goal file is the entire prompt the implementer receives, so it must be self-contained. The filename determines the branch name (`GOAL-my-change.md` → branch `goal-my-change`).

## Claude Code Skills

Installing dvx also installs `/dvx:*` skills for Claude Code. Use them directly in Claude Code:

```
/dvx:help      # Show all available dvx skills
/dvx:status    # Show current orchestration state
```

The orchestration skills (implement, review, finalize, deslop, etc.) are used internally by `dvx run`.

## How It Works

1. Write a plan file (any markdown format - dvx uses Claude to parse it)
2. Run `dvx run PLAN-*.md`
3. For each task, dvx:
   - Runs an **implementer** Claude session to write code
   - Runs a **reviewer** Claude session to check the work
   - Iterates if issues found (up to 3 times)
   - Commits when approved
4. If blocked, `dvx run` launches an interactive session to resolve

## Plan Files

Use `dvx plan` to generate plans with Claude:

```bash
# Piped input with explicit filename
echo "Build a REST API for user management with CRUD operations" | dvx plan PLAN-user-api.md

# Interactive editor, Claude names the file
dvx plan

# Update an existing plan
echo "Add rate limiting to the API" | dvx plan PLAN-user-api.md
```

Plans can be any markdown format - dvx uses Claude to extract tasks:

```markdown
# My Feature Plan

## Phase 1: Setup
Create the basic structure...

## Phase 2: Implementation
Build the core functionality...

## Phase 3: Testing
Add tests for...
```

## Project State

dvx stores state in `.dvx/` in your project:
- `<plan-file>/state.json` - Orchestration state for that plan
- `<plan-file>/blocked-context.md` - Context when blocked
- `<plan-file>/DECISIONS-*.md` - Decisions made by Claude
- `<plan-file>/dvx.log` - Debug log
- `task-status.json` - Task completion status (all plans)
- `watch/` - Goal watcher state

## License

Apache 2.0
