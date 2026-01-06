#!/usr/bin/env python3
"""
dvx - Development Orchestrator CLI.

Automates the implement → review → test → commit development loop.
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from claude_session import launch_interactive, run_claude
from orchestrator import run_orchestrator
from plan_parser import (
    clear_status_for_plan,
    get_next_pending_task,
    get_plan_summary,
    sync_plan_state,
)
from state import (
    Phase,
    clear_blocked,
    get_decisions,
    get_dvx_dir,
    get_dvx_root,
    load_state,
    save_state,
    update_phase,
)


def get_user_input_from_editor() -> str:
    """Open $EDITOR to get user input, return the text."""
    import os
    import subprocess
    import tempfile

    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Describe your plan\n\n# Delete this line and write your plan description here.\n# Save and exit when done.\n")
        temp_path = f.name

    try:
        subprocess.run([editor, temp_path], check=True)
        with open(temp_path, "r") as f:
            content = f.read()
        # Remove the template lines if unchanged
        lines = [line for line in content.split("\n") if not line.startswith("#")]
        return "\n".join(lines).strip()
    finally:
        os.unlink(temp_path)


def cmd_plan(args) -> int:
    """
    Generate or update a plan file using Claude Code ultrathink mode.

    Input can be piped or entered via editor.
    If plan_file is provided, uses that name; otherwise Claude suggests one.
    """
    import sys

    plan_file = args.plan_file if hasattr(args, "plan_file") and args.plan_file else None

    # Get input: piped or editor
    if not sys.stdin.isatty():
        # Input is piped
        user_input = sys.stdin.read().strip()
    else:
        # Open editor
        print("Opening editor to capture plan description...")
        user_input = get_user_input_from_editor()

    if not user_input:
        print("Error: No input provided.")
        return 1

    print("Generating plan with Claude ultrathink...")
    print()

    # Check if updating existing file
    existing_content = ""
    if plan_file and Path(plan_file).exists():
        existing_content = Path(plan_file).read_text()
        action = "update"
    else:
        action = "create"

    # Build the prompt
    if action == "update":
        prompt = f"""You are updating an existing plan file.

EXISTING PLAN:
{existing_content}

USER'S REQUEST:
{user_input}

Update the plan file based on the user's request. Maintain the existing structure where appropriate.
Output ONLY the complete updated plan file content, no explanations.
"""
    else:
        if plan_file:
            filename_instruction = f"The file will be named: {plan_file}"
        else:
            filename_instruction = """At the very end of your response, on a new line, output ONLY:
FILENAME: PLAN-<descriptive-name>.md

Choose a descriptive name based on the plan content."""

        prompt = f"""Write a detailed implementation plan in markdown format.

REQUEST: {user_input}

REQUIREMENTS:
- Start with "# Plan: <title>" header
- Include "## Overview" section explaining the goal
- Break into "## Phase N: Title" sections
- Be EXTREMELY detailed in each phase:
  - Include complete code examples with types and functions
  - Show data structures with all fields
  - Include SQL queries where relevant
  - Specify file paths and directory structures
  - Write algorithm implementations, not just descriptions
- Each phase must be comprehensive enough to implement without clarification

{filename_instruction}

CRITICAL: Output ONLY the raw markdown plan. Start immediately with "# Plan:" - no preamble, no summary, no "I've created", no conversational text.
"""

    # Run Claude with Opus for deep thinking on plans
    result = run_claude(prompt, timeout=300, model='opus')

    if not result.success:
        print(f"Error: Claude failed - {result.block_reason or 'unknown error'}")
        return 1

    output = result.output.strip()

    # Extract filename if Claude suggested one
    if not plan_file:
        lines = output.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("FILENAME:"):
                plan_file = line.replace("FILENAME:", "").strip()
                # Remove the FILENAME line from output
                output = "\n".join(lines[:i]).strip()
                break

        if not plan_file:
            plan_file = "PLAN-new.md"

    # Write the plan file
    Path(plan_file).write_text(output + "\n")
    print(f"{'Updated' if action == 'update' else 'Created'}: {plan_file}")

    # Show summary
    line_count = len(output.split("\n"))
    print(f"  {line_count} lines")

    return 0


class VerboseLogFilter(logging.Filter):
    """Filter to exclude verbose internal logs from console output.

    These logs go to the file handler but not the console, keeping
    CLI output clean and focused on orchestrator progress.
    """

    # Modules whose logs should only go to file, not console
    FILE_ONLY_MODULES = {"claude_session", "plan_parser"}

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name not in self.FILE_ONLY_MODULES


def setup_logging(verbose: bool = False, plan_file: Optional[str] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"

    # Console handler - filtered to exclude verbose internal logs
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(logging.Formatter(format_str))
    if not verbose:
        console_handler.addFilter(VerboseLogFilter())

    logging.basicConfig(
        level=level,
        handlers=[console_handler],
    )

    # Set up file logging in plan-specific or root .dvx directory
    if plan_file:
        dvx_dir = get_dvx_dir(plan_file)
        dvx_dir.mkdir(parents=True, exist_ok=True)
    else:
        dvx_dir = get_dvx_root()
        dvx_dir.mkdir(exist_ok=True)

    # File handler - gets ALL logs including claude_session
    log_file = dvx_dir / "dvx.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)


def check_git_environment() -> tuple[bool, str]:
    """
    Verify we're in a git repo and not on main/master branch.

    Returns: (ok, error_message)
    """
    import subprocess

    # Check if we're in a git repository
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Not in a git repository. dvx requires a git-managed project."

    # Get current branch
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "Could not determine current git branch."

    branch = result.stdout.strip()

    if branch in ("main", "master"):
        return False, f"""Cannot run dvx on '{branch}' branch.

dvx should be run on a feature branch, not directly on {branch}.

To fix this:
  1. Create a new branch:  git checkout -b feature/my-feature
  2. Then run:             dvx run <plan-file>

This ensures all changes are isolated and can be reviewed before merging."""

    return True, ""


def cmd_run(args) -> int:
    """
    Run orchestration - handles all states automatically.

    - No state: starts fresh with the plan file
    - Blocked: launches interactive Claude session to resolve, then continues
    - Paused: continues to next task
    - In progress: continues orchestration
    """
    # Verify git environment
    ok, error = check_git_environment()
    if not ok:
        print(f"Error: {error}")
        return 1

    plan_file = str(Path(args.plan_file))

    if not Path(plan_file).exists():
        print(f"Error: Plan file not found: {plan_file}")
        return 1

    # Check state first - skip sync for blocked/paused/complete states
    state = load_state(plan_file)
    step_mode = args.step

    # Handle blocked state - launch interactive session to resolve
    if state is not None and state.phase == Phase.BLOCKED.value:
        print(f"Resuming blocked orchestration: {state.plan_file}")
        print(f"Current task: {state.current_task_id} - {state.current_task_title}")
        print()

        blocked_file = get_dvx_dir(plan_file) / "blocked-context.md"
        blocked_context = ""
        if blocked_file.exists():
            blocked_context = blocked_file.read_text()
            print("Blocked context:")
            print("-" * 40)
            lines = blocked_context.split('\n')[:15]
            print('\n'.join(lines))
            if len(blocked_context.split('\n')) > 15:
                print("...")
            print("-" * 40)
            print()

        print("Launching interactive Claude session to resolve...")
        print("Type /exit when done to return to dvx.")
        print()

        # Start a FRESH session with just the blocked context (not the accumulated overseer session)
        # This avoids context bloat from previous tasks
        initial_prompt = f"""You are helping resolve a blocked dvx orchestration.

## Current Task

**Task {state.current_task_id}**: {state.current_task_title}

## Plan File

{state.plan_file}

## Issue

{blocked_context}

## Instructions

When explaining the blocking reason:
1. Summarize WHY this was blocked (1-2 sentences)
2. List the specific issues that need to be addressed
3. Ask if the user wants you to start fixing them

After explaining, wait for user direction before taking action.

## IMPORTANT: Before User Types /exit

When the user indicates the task is complete (or before they type /exit):
1. **Update the plan file**: Mark the task as complete with [x] or ✅
2. **Commit changes**: Stage and commit all changes for this task
3. Confirm to the user that the task is marked complete

This ensures dvx knows to move to the next task instead of re-implementing this one.
"""
        launch_interactive(initial_prompt=initial_prompt, plan_file=plan_file)

        print()
        print("Interactive session ended.")
        print("Clearing blocked state and continuing...")
        print()

        clear_blocked(plan_file)

        # Sync state with plan file BEFORE continuing orchestration
        # The interactive session may have completed the task and updated the plan
        print(f"Syncing plan state: {plan_file}")
        sync_result = sync_plan_state(plan_file)
        if sync_result['synced'] > 0 or sync_result['added'] > 0:
            print(f"  Updated: {sync_result['synced']} synced, {sync_result['added']} added from plan markers")

        return run_orchestrator(state.plan_file, step_mode=state.step_mode)

    # === PLANNER: Sync state with plan file ===
    # This ensures the status tracking file matches the plan's [x] markers.
    # Handles cases where: user manually updated plan, escalator completed tasks,
    # or dvx clean was run but status file wasn't properly cleared.
    # Skip for paused/complete states since we're just resuming.
    if state is None or state.phase not in (Phase.PAUSED.value, Phase.COMPLETE.value):
        print(f"Syncing plan state: {plan_file}")
        sync_result = sync_plan_state(plan_file)
        if sync_result['synced'] > 0 or sync_result['added'] > 0:
            print(f"  Updated: {sync_result['synced']} synced, {sync_result['added']} added from plan markers")

    if state is None:
        print(f"Starting orchestration of: {plan_file}")
        print()

        summary = get_plan_summary(plan_file)
        print(f"Plan: {summary['total']} tasks")
        print(f"  Done: {summary['done']}")
        print(f"  In Progress: {summary['in_progress']}")
        print(f"  Pending: {summary['pending']}")
        print()

        next_task = get_next_pending_task(plan_file)
        if not next_task:
            print("No pending tasks found!")
            return 0

        if step_mode:
            print("Step mode: Will pause after each task for review")
            print()

        print(f"Starting with task {next_task.id}: {next_task.title}")
        print("=" * 60)
        print()

        return run_orchestrator(plan_file, step_mode=step_mode)

    elif state.phase == Phase.PAUSED.value:
        print(f"Resuming from step-mode pause: {state.plan_file}")
        print()

        update_phase(Phase.IDLE, plan_file)
        return run_orchestrator(state.plan_file, step_mode=state.step_mode)

    elif state.phase == Phase.COMPLETE.value:
        print(f"Plan already complete: {state.plan_file}")
        summary = get_plan_summary(state.plan_file)
        print(f"All {summary['total']} tasks done!")
        return 0

    else:
        print(f"Continuing orchestration of: {state.plan_file}")
        print(f"Phase: {state.phase}")
        print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
        print()

        if step_mode and not state.step_mode:
            state.step_mode = True
            save_state(state)
            print("Step mode enabled")
            print()

        return run_orchestrator(state.plan_file, step_mode=state.step_mode)


def cmd_status(args) -> int:
    """Show current orchestration status for a plan."""
    plan_file = args.plan_file
    state = load_state(plan_file)

    if state is None:
        print(f"No active orchestration for: {plan_file}")
        print(f"Use 'dvx run {plan_file}' to begin.")
        return 0

    print(f"DVX Status: {plan_file}")
    print("=" * 40)
    print(f"Phase: {state.phase}")
    print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
    print(f"Iteration: {state.iteration_count}/{state.max_iterations}")
    print(f"Step mode: {'yes' if state.step_mode else 'no'}")
    print(f"Started: {state.started_at}")
    print(f"Updated: {state.updated_at}")
    print()

    if state.phase == Phase.PAUSED.value:
        print("PAUSED - Task completed, waiting for review")
        print(f"Run 'dvx run {plan_file}' to continue.")
    elif state.phase == Phase.BLOCKED.value:
        blocked_file = get_dvx_dir(plan_file) / "blocked-context.md"
        print(f"BLOCKED - See {blocked_file} for details")
        print(f"Run 'dvx run {plan_file}' to resolve and continue.")
    else:
        try:
            summary = get_plan_summary(state.plan_file)
            print(f"Progress: {summary['done']}/{summary['total']} tasks complete")
        except FileNotFoundError:
            print(f"Warning: Plan file not found: {state.plan_file}")

    return 0


def cmd_decisions(args) -> int:
    """Show decisions made during orchestration for a plan."""
    plan_file = args.plan_file
    decision_files = get_decisions(plan_file)

    if not decision_files:
        print(f"No decisions recorded for: {plan_file}")
        return 0

    print(f"Decisions for: {plan_file}")
    print("=" * 40)

    for decision_file in decision_files:
        print(f"\n{decision_file.name}:")
        print("-" * 40)
        print(decision_file.read_text())

    return 0


def cmd_clean(args) -> int:
    """Delete .dvx/ directory or plan-specific subdirectory and clear related state."""
    import shutil

    from plan_parser import clear_cache, clear_status

    plan_file = args.plan_file if hasattr(args, 'plan_file') and args.plan_file else None

    if plan_file:
        # Clean specific plan
        dvx_dir = get_dvx_dir(plan_file)
        if dvx_dir.exists():
            shutil.rmtree(dvx_dir)
            print(f"Removed {dvx_dir}")
        else:
            print(f"No state directory for: {plan_file}")

        # Also clear status and cache for this plan
        # This ensures a clean restart when running again
        if Path(plan_file).exists():
            clear_status_for_plan(plan_file)
            print(f"Cleared task statuses for: {plan_file}")
    else:
        # Clean entire .dvx directory
        dvx_dir = get_dvx_root()
        if dvx_dir.exists():
            shutil.rmtree(dvx_dir)
            print(f"Removed {dvx_dir}")
        else:
            print("No .dvx/ directory to clean.")

        # Also clear all caches and statuses
        clear_cache()
        clear_status()
        print("Cleared all caches and statuses")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dvx",
        description="Development Orchestrator - Automated implement/review/test/commit cycles",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", help="Run orchestration (start, continue, or resolve)")
    run_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    run_parser.add_argument("-f", "--force", action="store_true", help="Force restart with new plan file")
    run_parser.add_argument("-s", "--step", action="store_true", help="Step mode: pause after each task for review")
    run_parser.set_defaults(func=cmd_run)

    # status
    status_parser = subparsers.add_parser("status", help="Show current status for a plan")
    status_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    status_parser.set_defaults(func=cmd_status)

    # decisions
    decisions_parser = subparsers.add_parser("decisions", help="Show decisions made for a plan")
    decisions_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    decisions_parser.set_defaults(func=cmd_decisions)

    # clean
    clean_parser = subparsers.add_parser("clean", help="Delete .dvx/ directory (all) or plan-specific state")
    clean_parser.add_argument("plan_file", nargs="?", help="Path to PLAN-*.md file (optional, cleans all if omitted)")
    clean_parser.set_defaults(func=cmd_clean)

    # plan
    plan_parser = subparsers.add_parser("plan", help="Generate or update a plan file with Claude ultrathink")
    plan_parser.add_argument("plan_file", nargs="?", help="Path to PLAN-*.md file (optional)")
    plan_parser.set_defaults(func=cmd_plan)

    args = parser.parse_args()

    # Get plan_file for logging if available
    plan_file = getattr(args, 'plan_file', None)
    setup_logging(args.verbose, plan_file)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
