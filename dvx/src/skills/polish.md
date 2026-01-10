---
category: dvx
name: polish
description: Perform a holistic review of all changes after task completion
arguments:
  - name: plan_file
    description: Path to the PLAN file
    required: true
  - name: plan_content
    description: The plan file content
    required: true
  - name: git_diff
    description: The git diff of all changes
    required: true
---

# Polisher Role

You are performing a final polish review of all changes made for plan file $ARGUMENTS.plan_file.

All tasks have been implemented and reviewed individually. Now step back and review the ENTIRE implementation holistically.

## All Changes

```
$ARGUMENTS.git_diff
```

## Plan Summary

$ARGUMENTS.plan_content

## Review Focus

Look at the implementation as a whole and consider:

### 1. Architecture & Design
- Does the overall design make sense?
- Are there any patterns that could be improved?
- Is there unnecessary complexity that could be simplified?

### 2. Consistency
- Is naming consistent across the implementation?
- Are similar operations handled similarly?
- Does the code follow a coherent style?

### 3. Edge Cases & Error Handling
- Are edge cases handled appropriately?
- Is error handling consistent and informative?
- Are there any failure modes not considered?

### 4. Documentation & Clarity
- Is the code self-documenting?
- Are complex sections adequately commented?
- Would a new developer understand this code?

### 5. Polish Opportunities
- Are there quick wins that would improve quality?
- Any dead code or unused imports to clean up?
- Any obvious refactoring that would help?

## Categorize Your Findings

Sort improvements into two buckets:

### Quick Wins (Implement Now)
- Single-file fixes
- Simple additions (< 20 lines)
- Straightforward cleanup (dead code, unused imports)
- Minor refactors with obvious implementation

### Deferred Work (Create FIX Files)
- Multi-file refactors
- Changes requiring design decisions
- Performance optimizations needing measurement
- Larger improvements that could introduce risk

## Output Format

If everything looks polished and ready:
```
[POLISHED]

Brief summary of why the implementation is ready.
```

If there are improvements to make:
```
[SUGGESTIONS]

## Quick Wins (Implement Now)

1. [Description]
   - File: path/to/file
   - What to change
   - Priority: HIGH/MEDIUM/LOW

## Deferred Work (Create FIX Files)

1. [Title for FIX file]
   - Problem: What needs improving
   - Solution: How to fix it
   - Files affected: list of files
   - Priority: HIGH/MEDIUM/LOW
```

## Notes

- Be conservative: when in doubt, defer to a FIX file
- Quick wins should be low-risk, obvious improvements
- FIX files capture valuable improvements without risking the release
- Focus on meaningful improvements, not nitpicks
- Be specific and actionable
