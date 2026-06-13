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
| `/dvx:finalize` | Final quality gate before merge |
| `/dvx:deslop` | Clean up LLM-written prose artifacts in changed files |
| `/dvx:commit-task` | Mark a task complete and commit changes |
| `/dvx:add-tests` | Add missing tests for a task implementation |

### Planning Skills (Used by `dvx plan`, `dvx interview`, and `dvx autopilot`)

| Skill | Description |
|-------|-------------|
| `/dvx:create-plan` | Create a new implementation plan from requirements |
| `/dvx:update-plan` | Update an existing plan file with changes |
| `/dvx:interview` | Socratic clarification loop that turns a vague task into a spec |
| `/dvx:consensus-planner` | Planner role in the consensus loop |
| `/dvx:architect` | Architect role in the consensus loop (steelman challenges) |
| `/dvx:critic` | Critic role in the consensus loop (final verdict) |

### Interactive Skills (For Humans)

| Skill | Description |
|-------|-------------|
| `/dvx:help` | Show this help message |
| `/dvx:status` | Show current orchestration state |
| `/dvx:resolve-blocked` | Help resolve a blocked dvx orchestration |
| `/dvx:create-list` | Create a YAML queue file for batch dvx runs |

## CLI Commands

The `dvx` CLI provides these commands:

| Command | Description |
|---------|-------------|
| `dvx plan <file>` | Create or update a PLAN file |
| `dvx run <plan>` | Start or resume orchestration |
| `dvx run <queue.yaml>` | Run multiple plans sequentially from a YAML queue |
| `dvx interview <task>` | Run a deep-interview session that produces an execution-ready spec |
| `dvx autopilot <task>` | Sequence interview → consensus plan → run end-to-end |
| `dvx watch` | Watch the goals directory; GOAL*.md uses /goal, other files use dvx run |
| `dvx clear` | Clear watch-processing state (leaves the watched directory untouched) |
| `dvx status <plan>` | Show current status |
| `dvx decisions <plan>` | Show decisions made during execution |
| `dvx clean [plan]` | Clean up state for a plan (or all state if omitted) |

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

## Queue File Format

Run multiple plans sequentially with a YAML queue file:

```yaml
- plans/FIX-bug1.md
- plans/FIX-bug2.md
- plans/FIX-bug3.md
```

Use `/dvx:create-list` to generate queue files, then `dvx run queue.yaml`.

## State Directory

DVX stores state in `.dvx/<plan-name>/`:
- `state.json` - Current orchestration state
- `blocked-context.md` - Context when blocked
- `DECISIONS.md` - Logged decisions

## More Information

See the README at https://github.com/dvx-sh/dvx-cli for full documentation.
