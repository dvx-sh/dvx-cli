---
category: dvx
name: implement-fix
description: Address review feedback for a task
arguments:
  - name: task_id
    description: The task ID being fixed
    required: true
  - name: task_title
    description: The task title
    required: true
  - name: task_description
    description: The full task description
    required: true
  - name: plan_file
    description: Path to the PLAN file
    required: true
  - name: feedback
    description: The review feedback to address
    required: true
---

# Implementer Role - Addressing Review Feedback

You are addressing review feedback for task $ARGUMENTS.task_id from plan file $ARGUMENTS.plan_file.

## Task

**$ARGUMENTS.task_id: $ARGUMENTS.task_title**

$ARGUMENTS.task_description

## Review Feedback

The reviewer has provided the following feedback that needs to be addressed:

---

$ARGUMENTS.feedback
---

## Instructions

1. **Carefully read** the feedback above.

2. **Address each point**:
   - If the feedback is valid, make the suggested changes
   - If you disagree with specific feedback, explain why in your output (but still consider if there's a middle ground)
   - If feedback is unclear, make your best interpretation

3. **Run tests** after making changes to ensure nothing is broken.

4. **Do NOT commit** - that happens after the next review.

## Notes

- Focus on addressing the specific feedback
- Don't make unrelated changes
- Keep the fix minimal and targeted
- If the feedback reveals a larger issue, note it but stay focused on this task
