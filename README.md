# dvx

Claude Code orchestrator that automates implement/review/test/commit cycles.

## Requirements

- Python 3.10+
- Claude Code CLI installed and authenticated

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/dvx-sh/dvx-cli/main/install-remote.sh | bash
```

Or clone and install locally:

```bash
git clone https://github.com/dvx-sh/dvx-cli.git
cd dvx-cli
./install.sh
```

Add to your shell config (~/.bashrc, ~/.zshrc, etc.):

```bash
export PATH="${HOME}/.dvx/bin:$PATH"
```

## Usage

```bash
# Generate a plan with Claude ultrathink
echo "Create a user authentication system" | dvx plan PLAN-auth.md
dvx plan                       # Opens editor, Claude names the file

# Run orchestration
dvx run PLAN-feature.md        # Run orchestration
dvx run -s PLAN-feature.md     # Step mode: pause after each task
dvx run --force PLAN-feature.md  # Force restart with new plan

dvx status                     # Show current status
dvx decisions                  # Show decisions made
dvx clean                      # Delete .dvx/ directory
```

The `run` command handles everything automatically:
- **No state**: Starts fresh orchestration
- **Blocked**: Launches interactive Claude session to resolve, then continues
- **Paused** (step mode): Continues to next task
- **Complete**: Shows completion message

## Claude Code Skills

Installing dvx also installs `/dvx:*` skills for Claude Code. Use them directly in Claude Code:

```
/dvx:help      # Show all available dvx skills
/dvx:status    # Show current orchestration state
```

The orchestration skills (implement, review, polish, finalize, etc.) are used internally by `dvx run`.

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

Use `dvx plan` to generate plans with Claude ultrathink mode:

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
- `state.json` - Current orchestration state
- `task-status.json` - Task completion status
- `blocked-context.md` - Context when blocked
- `DECISIONS-*.md` - Decisions made by Claude
- `dvx.log` - Debug log

## License

Apache 2.0
