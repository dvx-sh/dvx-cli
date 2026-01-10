---
category: dvx
name: status
description: Show current DVX orchestration state
arguments:
  - name: plan_file
    description: Optional path to a specific PLAN file
    required: false
---

# DVX Status

Show the current state of DVX orchestration.

## Instructions

1. **Find the plan file**:
   {{#if plan_file}}
   Use the specified plan file: $ARGUMENTS.plan_file
   {{else}}
   Look for active plans:
   - Check `.dvx/` directory for state files
   - Look for `plans/PLAN-*.md` files
   - Check git branch name for hints (e.g., `ddanieli/implement-foo`)
   {{/if}}

2. **Read the state**:
   - Look in `.dvx/<plan-name>/state.json` for orchestration state
   - Check `.dvx/task-status.json` for task completion status
   - Look for `blocked-context.md` if in blocked state

3. **Report the status**:

## Output Format

```
## DVX Status

**Plan**: [plan file path]
**Phase**: [idle/implementing/reviewing/blocked/complete]
**Current Task**: [task id and title, if any]

### Task Progress

| Task | Status |
|------|--------|
| 1. Task title | done/in_progress/pending/blocked |
| 2. Task title | done/in_progress/pending/blocked |
| ... | ... |

### Summary

- Total: X tasks
- Done: X
- In Progress: X
- Pending: X
- Blocked: X

### Next Action

[What should happen next - e.g., "Run `dvx run <plan>` to continue" or "Resolve blocked issue and run again"]
```

## If No Active Plan

If there's no active orchestration:

```
## DVX Status

No active orchestration found.

To start:
1. Create a plan file: `plans/PLAN-feature-name.md`
2. Run: `dvx run plans/PLAN-feature-name.md`

Or check for existing plans:
- `ls plans/PLAN-*.md`
```

## Notes

- This skill reads state but does not modify it
- For detailed decision history, use `/dvx:decisions`
- To resume a blocked state, use `dvx run <plan>` from the CLI
