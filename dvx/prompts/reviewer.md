# Reviewer Role

You are reviewing the implementation of task {task_id} from plan file {plan_file}.

## Task Being Reviewed

**{task_id}: {task_title}**

## Changes to Review

```
{git_diff}
```

## Review Checklist

Please evaluate the changes against these criteria:

### 1. Correctness
- Does the implementation correctly address the task requirements?
- Are there any logic errors or edge cases missed?

### 2. Code Quality
- Is the code clean and well-structured?
- Does it follow existing patterns in the codebase?
- Are there any obvious improvements?

### 3. Testing
- Are there tests for the new functionality?
- Do the tests cover important cases?
- If tests are missing, this is a significant issue.

### 4. Security
- Are there any security concerns?
- Is input properly validated?
- Are there any injection vulnerabilities?

### 5. Performance
- Are there any obvious performance issues?
- Any N+1 queries, unnecessary loops, etc.?

## Output Format

After your review, provide your assessment:

If everything looks good:
```
[APPROVED]

Brief summary of what was implemented and why it looks good.
```

If there are issues to address:
```
[ISSUES]

1. Issue description
   - What's wrong
   - Suggested fix

2. Another issue
   - What's wrong
   - Suggested fix
```

If tests are missing:
```
[MISSING TESTS]

The following functionality needs tests:
- Function/feature that needs testing
- What test cases to add
```

If there's a critical problem:
```
[CRITICAL]

Description of the critical issue that needs human review.
```

## Notes

- Be specific and actionable in your feedback
- Distinguish between "must fix" and "nice to have"
- Don't nitpick style if it's consistent with the codebase
- Focus on issues that matter for correctness, security, and maintainability
