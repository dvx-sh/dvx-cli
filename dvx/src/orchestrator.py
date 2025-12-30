"""
Main orchestrator loop for dvx.

Coordinates the implement → review → fix → test → commit cycle.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional
import logging

from claude_session import run_claude, SessionResult
from plan_parser import (
    get_next_pending_task, update_task_status, TaskStatus, Task,
    get_plan_summary,
)
from state import (
    State, Phase, load_state, save_state, create_initial_state,
    update_phase, set_current_task, increment_iteration,
    set_overseer_session, write_blocked_context, log_decision,
    ensure_dvx_dir,
)

logger = logging.getLogger(__name__)

# Path to prompts directory
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def parse_decisions(output: str) -> list[dict]:
    """
    Parse decision markers from Claude's output.

    Looks for:
    [DECISION: topic]
    Decision: What was decided
    Reasoning: Why
    Alternatives: What else was considered
    """
    import re

    decisions = []
    pattern = r'\[DECISION:\s*([^\]]+)\]\s*Decision:\s*(.+?)\s*Reasoning:\s*(.+?)\s*Alternatives:\s*(.+?)(?=\[DECISION:|$)'

    matches = re.findall(pattern, output, re.DOTALL | re.IGNORECASE)
    for match in matches:
        topic, decision, reasoning, alternatives = match
        # Parse alternatives (usually a list)
        alt_list = [a.strip().lstrip('- ') for a in alternatives.strip().split('\n') if a.strip()]
        decisions.append({
            'topic': topic.strip(),
            'decision': decision.strip(),
            'reasoning': reasoning.strip(),
            'alternatives': alt_list,
        })

    return decisions


def log_decisions_from_output(output: str) -> None:
    """Parse and log any decisions from Claude's output."""
    decisions = parse_decisions(output)
    for d in decisions:
        log_decision(
            topic=d['topic'],
            decision=d['decision'],
            reasoning=d['reasoning'],
            alternatives=d['alternatives'],
        )
        logger.info(f"Logged decision: {d['topic']}")


def load_prompt(name: str) -> str:
    """Load a prompt template from the prompts directory."""
    prompt_file = PROMPTS_DIR / f"{name}.md"
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt template not found: {prompt_file}")
    return prompt_file.read_text()


def get_git_diff() -> str:
    """Get the current git diff for review."""
    try:
        # Get staged and unstaged changes
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = result.stdout

        # Also get untracked files
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = result.stdout

        return f"Git Status:\n{status}\n\nGit Diff:\n{diff}"
    except Exception as e:
        logger.error(f"Error getting git diff: {e}")
        return f"Error getting git diff: {e}"


def run_implementor(task: Task, plan_file: str, feedback: Optional[str] = None) -> SessionResult:
    """
    Run a fresh implementor session for a task.

    Args:
        task: The task to implement
        plan_file: Path to the plan file
        feedback: Optional feedback from reviewer to address
    """
    logger.info(f"Running implementor for task {task.id}: {task.title}")

    prompt_template = load_prompt("implementor")
    if feedback:
        prompt_template = load_prompt("implementor-fix")

    # Build the prompt
    prompt = prompt_template.format(
        task_id=task.id,
        task_title=task.title,
        task_description=task.description,
        plan_file=plan_file,
        feedback=feedback or "",
    )

    return run_claude(prompt)


def run_reviewer(plan_file: str, task: Task, overseer_session_id: Optional[str] = None) -> tuple[SessionResult, Optional[str]]:
    """
    Run the reviewer (overseer) to review changes.

    Returns: (result, session_id)
    """
    logger.info("Running reviewer...")

    prompt_template = load_prompt("reviewer")
    git_diff = get_git_diff()

    prompt = prompt_template.format(
        task_id=task.id,
        task_title=task.title,
        plan_file=plan_file,
        git_diff=git_diff,
    )

    result = run_claude(prompt, session_id=overseer_session_id)

    return result, result.session_id


def parse_review_result(output: str) -> dict:
    """
    Parse the reviewer's output to determine next action.

    Returns dict with:
        - approved: bool
        - has_issues: bool
        - missing_tests: bool
        - suggestions: str
        - critical: bool
    """
    output_lower = output.lower()

    # Check for approval
    approved = (
        "[approved]" in output_lower or
        "lgtm" in output_lower or
        "looks good" in output_lower
    )

    # Check for issues
    has_issues = (
        "[issues]" in output_lower or
        "[suggestions]" in output_lower or
        "should be" in output_lower or
        "consider" in output_lower or
        "recommend" in output_lower
    )

    # Check for missing tests
    missing_tests = (
        "missing test" in output_lower or
        "no test" in output_lower or
        "add test" in output_lower or
        "needs test" in output_lower
    )

    # Check for critical issues
    critical = (
        "[critical]" in output_lower or
        "[blocked]" in output_lower or
        "critical issue" in output_lower or
        "security" in output_lower
    )

    return {
        "approved": approved and not has_issues and not critical,
        "has_issues": has_issues,
        "missing_tests": missing_tests,
        "suggestions": output,  # Full output as suggestions
        "critical": critical,
    }


def run_implementor_commit(task: Task, plan_file: str) -> SessionResult:
    """Tell the implementor to update the plan and commit."""
    logger.info("Running implementor commit...")

    prompt = f"""The implementation for task {task.id} ({task.title}) has been reviewed and approved.

Please:
1. Update the plan file ({plan_file}) to mark task {task.id} as complete (change [ ] to [x])
2. Stage ONLY your changes (not other sessions' work if any)
3. Create a commit with a meaningful message explaining why these changes were made

Remember:
- Only commit files you modified for this task
- The plan file should be included in the commit
- Use a descriptive commit message focused on the "why" not the "what"
"""

    return run_claude(prompt)


def handle_blocked(state: State, reason: str, context: str, session_id: Optional[str] = None) -> int:
    """
    Handle a blocked state - write context for review.

    Args:
        state: Current orchestrator state
        reason: Why we're blocked
        context: Full context of the blockage
        session_id: Optional session ID from the blocking Claude session (overrides state.overseer_session_id)
    """
    logger.warning(f"Blocked: {reason}")

    update_phase(Phase.BLOCKED)
    # Use provided session_id, fall back to overseer session
    session_id = session_id or state.overseer_session_id
    blocked_file = write_blocked_context(reason, context, session_id=session_id, plan_file=state.plan_file)

    print()
    print("=" * 60)
    print("BLOCKED")
    print("=" * 60)
    print(f"Reason: {reason}")
    print()
    print(f"Context written to: {blocked_file}")
    print()
    print(f"Run `dvx run {state.plan_file}` and when resolved type `/exit` - dvx will continue.")
    print()

    return 1  # Exit with error to signal blocked state


def run_orchestrator(plan_file: str, step_mode: bool = False) -> int:
    """
    Main orchestration loop.

    Args:
        plan_file: Path to the plan file
        step_mode: If True, pause after each task completion for review

    Returns: 0 on success/completion, 1 on error/blocked/paused
    """
    try:
        return _run_orchestrator_inner(plan_file, step_mode)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        print("\nInterrupted. Run 'dvx continue' to resume.")
        return 1
    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        print(f"\nError: {e}")
        return 1
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        print(f"\nUnexpected error: {e}")
        print("Check .dvx/dvx.log for details.")
        return 1


def _run_orchestrator_inner(plan_file: str, step_mode: bool = False) -> int:
    """Inner orchestration loop (wrapped by run_orchestrator for error handling)."""
    # Ensure .dvx directory exists
    ensure_dvx_dir()

    # Load or create state
    state = load_state()
    if state is None:
        state = create_initial_state(plan_file)
        state.step_mode = step_mode
        save_state(state)
    elif state.plan_file != plan_file:
        logger.warning(f"Switching from {state.plan_file} to {plan_file}")
        state = create_initial_state(plan_file)
        state.step_mode = step_mode
        save_state(state)
    elif step_mode and not state.step_mode:
        # Enable step mode if requested
        state.step_mode = step_mode
        save_state(state)

    logger.info(f"Starting orchestration of {plan_file}")

    while True:
        # Get next task
        task = get_next_pending_task(plan_file)
        if task is None:
            # Check if we're done
            summary = get_plan_summary(plan_file)
            if summary['pending'] == 0 and summary['in_progress'] == 0:
                print()
                print("=" * 60)
                print("COMPLETE")
                print("=" * 60)
                print(f"All {summary['total']} tasks completed!")
                print()
                print("You may want to:")
                print("  - Review any decisions in .dvx/DECISIONS-*.md")
                print("  - Remove the plan file if no longer needed")
                print()
                update_phase(Phase.COMPLETE)
                return 0
            else:
                logger.warning("No pending tasks but plan not complete - check for blocked tasks")
                return 1

        # Set current task
        set_current_task(task.id, task.title)
        update_task_status(plan_file, task.id, TaskStatus.IN_PROGRESS)

        print()
        print(f"Task {task.id}: {task.title}")
        print("-" * 60)

        # === IMPLEMENTATION PHASE ===
        update_phase(Phase.IMPLEMENTING)
        logger.info(f"Implementing task {task.id}")

        impl_result = run_implementor(task, plan_file)

        # Log any decisions made during implementation
        log_decisions_from_output(impl_result.output)

        if not impl_result.success:
            return handle_blocked(
                state,
                impl_result.block_reason or "Implementation failed",
                impl_result.output,
                session_id=impl_result.session_id,
            )

        if impl_result.blocked:
            return handle_blocked(
                state,
                impl_result.block_reason or "Implementor is blocked",
                impl_result.output,
                session_id=impl_result.session_id,
            )

        # === REVIEW PHASE ===
        update_phase(Phase.REVIEWING)
        logger.info("Reviewing implementation...")

        review_result, session_id = run_reviewer(
            plan_file,
            task,
            state.overseer_session_id,
        )

        if session_id:
            set_overseer_session(session_id)

        if not review_result.success:
            return handle_blocked(
                state,
                "Review failed",
                review_result.output,
                session_id=review_result.session_id,
            )

        review = parse_review_result(review_result.output)

        # === FIX PHASE (if needed) ===
        iteration = 0
        while review['has_issues'] and not review['approved']:
            iteration += 1
            state, exceeded = increment_iteration()

            if exceeded:
                return handle_blocked(
                    state,
                    f"Max iterations ({state.max_iterations}) exceeded - review loop not converging",
                    f"Last review feedback:\n{review['suggestions']}",
                    session_id=session_id,
                )

            if review['critical']:
                return handle_blocked(
                    state,
                    "Critical issue found in review",
                    review['suggestions'],
                    session_id=session_id,
                )

            print(f"  Review iteration {iteration}: addressing feedback...")
            update_phase(Phase.FIXING)

            # Run implementor with feedback
            impl_result = run_implementor(task, plan_file, feedback=review['suggestions'])

            # Log any decisions made during fix
            log_decisions_from_output(impl_result.output)

            if not impl_result.success or impl_result.blocked:
                return handle_blocked(
                    state,
                    impl_result.block_reason or "Fix implementation failed",
                    impl_result.output,
                    session_id=impl_result.session_id,
                )

            # Re-review
            update_phase(Phase.REVIEWING)
            review_result, session_id = run_reviewer(
                plan_file,
                task,
                state.overseer_session_id,
            )

            if session_id:
                set_overseer_session(session_id)

            review = parse_review_result(review_result.output)

        # === TEST PHASE ===
        if review['missing_tests']:
            print("  Adding missing tests...")
            update_phase(Phase.TESTING)

            test_prompt = f"""The reviewer noted that tests are missing for task {task.id} ({task.title}).

Please add appropriate tests for the changes made. Consider:
- Unit tests for new functions/methods
- Integration tests if appropriate
- Edge cases and error handling

Run the tests after writing them to ensure they pass.
"""
            test_result = run_claude(test_prompt)

            if not test_result.success or test_result.blocked:
                return handle_blocked(
                    state,
                    test_result.block_reason or "Test writing failed",
                    test_result.output,
                    session_id=test_result.session_id,
                )

        # === COMMIT PHASE ===
        print("  Committing changes...")
        update_phase(Phase.COMMITTING)

        commit_result = run_implementor_commit(task, plan_file)

        if not commit_result.success or commit_result.blocked:
            return handle_blocked(
                state,
                commit_result.block_reason or "Commit failed",
                commit_result.output,
                session_id=commit_result.session_id,
            )

        # Mark task as done
        update_task_status(plan_file, task.id, TaskStatus.DONE)
        print(f"  Task {task.id} complete!")

        # Reset iteration count for next task
        state = load_state()
        state.iteration_count = 0
        save_state(state)

        # Step mode: pause after each task for review
        if state.step_mode:
            print()
            print("=" * 60)
            print("PAUSED (step mode)")
            print("=" * 60)
            print(f"Task {task.id} ({task.title}) completed and committed.")
            print()
            print("Review the changes, then run 'dvx continue' for next task.")
            print()
            update_phase(Phase.PAUSED)
            return 0  # Exit cleanly to allow review

        # Continue to next task...


if __name__ == "__main__":
    # For testing
    import sys
    if len(sys.argv) > 1:
        run_orchestrator(sys.argv[1])
    else:
        print("Usage: python orchestrator.py <plan-file>")
