---
category: dvx
name: resolve-blocked
description: Help resolve a blocked dvx orchestration
arguments:
  - name: plan_file
    description: Path to the plan file
    required: true
  - name: task_id
    description: The blocked task ID
    required: true
  - name: task_title
    description: The blocked task title
    required: true
  - name: blocked_reason
    description: Why the orchestration was blocked
    required: true
  - name: context
    description: Full context of the blocking issue
    required: true
---

# Resolve Blocked Orchestration

You are helping resolve a blocked dvx orchestration.

## Current Task

**Task $ARGUMENTS.task_id**: $ARGUMENTS.task_title

## Plan File

$ARGUMENTS.plan_file

## Blocking Issue

$ARGUMENTS.blocked_reason

## Context

$ARGUMENTS.context

## Instructions

### 1. Explain the Issue

When explaining the blocking reason:
1. Summarize WHY this was blocked (1-2 sentences)
2. List the specific issues that need to be addressed
3. Ask if the user wants you to start fixing them

After explaining, wait for user direction before taking action.

### 2. Resolution Strategies

Common resolution approaches:

- **Missing information**: Ask the user for clarification
- **Credential issues**: Guide the user to provide access
- **Architectural decisions**: Present options and let user choose
- **External dependencies**: Identify what's needed and how to proceed
- **Conflicting requirements**: Clarify priorities with the user

### 3. Before User Types /exit

**IMPORTANT**: When the user indicates the task is complete (or before they type /exit):

1. **Update the plan file**: Mark the task as complete with [x] or ✅
2. **Commit changes**: Stage and commit all changes for this task
3. Confirm to the user that the task is marked complete

This ensures dvx knows to move to the next task instead of re-implementing this one.

## Output Markers

Use these markers in your output:
- `[RESOLVED]` - When the issue is fixed
- `[NEEDS_USER_INPUT]` - When waiting for user response
- `[BLOCKED: reason]` - If a new blocking issue is found
