# Implementer Role - Addressing Review Feedback

You are addressing review feedback for task {task_id} from plan file {plan_file}.

## Task

**{task_id}: {task_title}**

{task_description}

## Review Feedback

The reviewer has provided the following feedback that needs to be addressed:

---

{feedback}
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
