---
category: dvx
name: help
description: Show all available dvx skills
---

# DVX Help

DVX is a development orchestration system that automates implement → review → commit cycles.

## Available Skills

### Orchestration Skills (Used by `dvx run`)

| Skill | Description |
|-------|-------------|
| `/dvx:implement` | Implement a single task from a plan file |
| `/dvx:implement-fix` | Address review feedback for a task |
| `/dvx:review` | Review the implementation of a task |
| `/dvx:escalate` | Evaluate a flagged situation and decide whether to proceed or escalate |
| `/dvx:split-task` | Analyze a task to determine if it should be split into subtasks |
| `/dvx:polish` | Perform a holistic review of all changes after task completion |
| `/dvx:finalize` | Final quality gate before merge |

### Interactive Skills (For Humans)

| Skill | Description |
|-------|-------------|
| `/dvx:help` | Show this help message |
| `/dvx:status` | Show current orchestration state |

## CLI Commands

The `dvx` CLI provides these commands:

| Command | Description |
|---------|-------------|
| `dvx plan <file>` | Create or update a PLAN file |
| `dvx run <plan>` | Start or resume orchestration |
| `dvx status` | Show current status |
| `dvx decisions` | Show decisions made during execution |
| `dvx clean <plan>` | Clean up state for a plan |

## Workflow

1. Create a PLAN file with tasks
2. Run `dvx run plans/PLAN-*.md`
3. DVX will implement, review, and commit each task
4. If blocked, resolve the issue and run again
5. When complete, merge the branch

## Plan File Format

```markdown
# PLAN: Feature Name

## Tasks

- [ ] Task 1: Description
- [ ] Task 2: Description
```

## State Directory

DVX stores state in `.dvx/<plan-name>/`:
- `state.json` - Current orchestration state
- `blocked-context.md` - Context when blocked
- `DECISIONS.md` - Logged decisions

## More Information

See the README at https://github.com/dvx-sh/dvx-cli for full documentation.
