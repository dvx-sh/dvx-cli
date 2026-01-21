---
category: dvx
name: commit-task
description: Mark a task complete and commit changes
arguments:
  - name: task_id
    description: The task ID that was completed (e.g., "1", "2.1")
    required: true
  - name: task_title
    description: The task title
    required: true
  - name: plan_file
    description: Path to the PLAN file
    required: true
---

# Commit Task Completion

The implementation for task $ARGUMENTS.task_id ($ARGUMENTS.task_title) has been reviewed and approved.

## Instructions

### 1. Update Plan File

Mark task $ARGUMENTS.task_id as complete in the plan file ($ARGUMENTS.plan_file):

- Change `[ ]` to `[x]`
- DO NOT add implementation notes, file lists, or patterns
- Add at most 1 brief line ONLY if implementation significantly differs from plan

### 2. Stage Only Your Changes

Stage ONLY files you modified for this task.

**IMPORTANT - Multiple sessions may be running:**
- Only commit files that YOU modified for THIS task
- Use `git status` to verify you're only committing your changes
- If you see changes you didn't make, leave them unstaged

### 3. Create Commit

Create a commit with a meaningful message explaining WHY the changes were made.

**Commit guidelines:**
- Include the plan file ($ARGUMENTS.plan_file) in the commit
- Focus the message on WHY not WHAT
- Use a clear, descriptive commit message

Example:
```
feat: add user session timeout handling

Sessions now expire after 30 minutes of inactivity to improve security.
Previously sessions lasted indefinitely which was a security concern.
```

## Why Keep Plan Files Lean

The plan is a TODO list, not documentation:
- Implementation details belong in commit messages and code
- New sessions can read the codebase to understand patterns
- Bloated plan files cause context overflow in later tasks
