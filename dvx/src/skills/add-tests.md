---
category: dvx
name: add-tests
description: Add missing tests for a task implementation
arguments:
  - name: task_id
    description: The task ID that needs tests
    required: true
  - name: task_title
    description: The task title
    required: true
  - name: reviewer_notes
    description: Reviewer's notes about what tests are missing
    required: true
---

# Add Missing Tests

The reviewer noted that tests are missing for task $ARGUMENTS.task_id ($ARGUMENTS.task_title).

## Reviewer's Notes

$ARGUMENTS.reviewer_notes

## Instructions

### 1. Analyze What Needs Testing

Read the implementation for task $ARGUMENTS.task_id to understand:
- What new code was added
- What functionality needs test coverage
- What edge cases exist

### 2. Write Appropriate Tests

Consider adding:
- **Unit tests** for new functions/methods
- **Integration tests** if the code interacts with external systems
- **Edge case tests** for boundary conditions
- **Error handling tests** to verify proper error responses

### 3. Follow Project Conventions

- Look at existing tests in the project for patterns
- Use the same testing framework and style
- Place tests in the appropriate directory

### 4. Run Tests

After writing tests, run them to ensure they pass:
- Run the specific new tests
- Run the full test suite to ensure no regressions

### 5. Output

When complete, provide a brief summary of:
- What tests were added
- What functionality they cover
- The test results
