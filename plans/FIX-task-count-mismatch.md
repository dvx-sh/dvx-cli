# FIX: Task Count Mismatch Between Start and Finish

**Priority:** Medium
**Component:** Plan Parser
**File:** `src/plan_parser.py`

## Problem

When dvx starts, it reports one task count (e.g., "5 tasks") but finishes with a different count (e.g., "All 2 tasks completed!").

Example output:
```
Plan: 5 tasks
  Done: 0
  Pending: 5

[executes 2 tasks]

All 2 tasks completed!
```

## Root Cause

Two issues combine to cause this:

### 1. Ambiguous Task Extraction Prompt

`_parse_with_claude` (line 261-296) tells Claude:

> Extract each distinct task/phase/step that represents a unit of work to be implemented.

This is too vague. Claude sometimes includes numbered items from "Files to Modify" sections as tasks, and sometimes doesn't.

### 2. Cache Invalidation Mid-Run

The cache key is content-based (line 56):
```python
cache_key = hashlib.md5(f"{filepath}:{content}".encode()).hexdigest()
```

When `commit-task` modifies the plan file (changing `[ ]` to `[x]`), the cache key changes. At finalization, `get_plan_summary` can't find the cached parse, so Claude re-parses. This second parse may yield a different task count.

## Implementation Tasks

- [ ] Update `_parse_with_claude` prompt to be explicit about what constitutes a task

## Fix Details

In `src/plan_parser.py`, update the prompt in `_parse_with_claude` (around line 261) to be explicit:

```python
prompt = f"""Analyze this plan file and extract all tasks that need to be implemented.

PLAN FILE CONTENT:
---
{content}
---

WHAT COUNTS AS A TASK:
1. Checkbox items: Lines starting with "- [ ]" or "- [x]" (unchecked/checked)
2. Items in sections explicitly named "Tasks", "Implementation Tasks", "Implementation Steps", or "Phases"
3. Numbered items (1., 2., etc.) ONLY if they describe work to implement

WHAT IS NOT A TASK:
- Items in "Files to Modify" sections (these are reference, not tasks)
- Items in "Testing" or "Verification" sections (these are QA steps, not implementation)
- Code examples or snippets
- Documentation or explanation text
- "Consider also" or optional suggestions

For each task, determine:
1. A unique ID (use the number from the plan, or generate sequential IDs)
2. A short title (the main heading or summary)
3. A description (the details/requirements for that task)
4. Status: Look for [x] = "done", [ ] = "pending", [IN_PROGRESS] = "in_progress", [BLOCKED] = "blocked"

Return ONLY valid JSON in this exact format (no markdown, no explanation):
...
```

## Testing

1. Create a plan file with:
   - 2 checkbox items in "Implementation Tasks"
   - 3 numbered items in "Files to Modify"
   - 5 numbered items in "Testing Verification"

2. Run `dvx run <plan>` and verify:
   - Initial count shows 2 tasks (only checkboxes)
   - Final count shows 2 tasks (consistent)

3. Manually modify the plan file mid-run and verify count stays consistent
