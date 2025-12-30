#!/usr/bin/env python3
"""
dvx - Development Orchestrator CLI.

Automates the implement → review → test → commit development loop.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from claude_session import launch_interactive, run_claude
from orchestrator import run_orchestrator
from plan_parser import get_next_pending_task, get_plan_summary
from state import (
    Phase,
    clear_blocked,
    get_decisions,
    get_dvx_dir,
    load_state,
    reset_state,
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


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"

    logging.basicConfig(
        level=level,
        format=format_str,
        handlers=[logging.StreamHandler()],
    )

    # Ensure .dvx/ exists and set up file logging
    dvx_dir = get_dvx_dir()
    dvx_dir.mkdir(exist_ok=True)

    log_file = dvx_dir / "dvx.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(file_handler)


def cmd_run(args) -> int:
    """
    Run orchestration - handles all states automatically.

    - No state: starts fresh with the plan file
    - Blocked: launches interactive Claude session to resolve, then continues
    - Paused: continues to next task
    - In progress: continues orchestration
    """
    plan_file = Path(args.plan_file)

    if not plan_file.exists():
        print(f"Error: Plan file not found: {plan_file}")
        return 1

    state = load_state()
    step_mode = args.step

    if state and state.plan_file != str(plan_file):
        if not args.force:
            print(f"Warning: Existing state is for different plan: {state.plan_file}")
            print(f"Use 'dvx run --force {plan_file}' to start fresh with new plan.")
            return 1
        reset_state()
        state = None

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

        return run_orchestrator(str(plan_file), step_mode=step_mode)

    elif state.phase == Phase.BLOCKED.value:
        print(f"Resuming blocked orchestration: {state.plan_file}")
        print(f"Current task: {state.current_task_id} - {state.current_task_title}")
        print()

        blocked_file = get_dvx_dir() / "blocked-context.md"
        if blocked_file.exists():
            print("Blocked context:")
            print("-" * 40)
            lines = blocked_file.read_text().split('\n')[:15]
            print('\n'.join(lines))
            if len(blocked_file.read_text().split('\n')) > 15:
                print("...")
            print("-" * 40)
            print()

        print("Launching interactive Claude session to resolve...")
        print("Type /exit when done to return to dvx.")
        print()

        launch_interactive(session_id=state.overseer_session_id)

        print()
        print("Interactive session ended.")
        print("Clearing blocked state and continuing...")
        print()

        clear_blocked()
        return run_orchestrator(state.plan_file, step_mode=state.step_mode)

    elif state.phase == Phase.PAUSED.value:
        print(f"Resuming from step-mode pause: {state.plan_file}")
        print()

        update_phase(Phase.IDLE)
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
    """Show current orchestration status."""
    state = load_state()

    if state is None:
        print("No active orchestration.")
        print("Use 'dvx run <plan-file>' to begin.")
        return 0

    print("DVX Status")
    print("=" * 40)
    print(f"Plan file: {state.plan_file}")
    print(f"Phase: {state.phase}")
    print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
    print(f"Iteration: {state.iteration_count}/{state.max_iterations}")
    print(f"Step mode: {'yes' if state.step_mode else 'no'}")
    print(f"Started: {state.started_at}")
    print(f"Updated: {state.updated_at}")
    print()

    if state.phase == Phase.PAUSED.value:
        print("PAUSED - Task completed, waiting for review")
        print("Run 'dvx run <plan-file>' to continue.")
    elif state.phase == Phase.BLOCKED.value:
        print("BLOCKED - See .dvx/blocked-context.md for details")
        print("Run 'dvx run <plan-file>' to resolve and continue.")
    else:
        try:
            summary = get_plan_summary(state.plan_file)
            print(f"Progress: {summary['done']}/{summary['total']} tasks complete")
        except FileNotFoundError:
            print(f"Warning: Plan file not found: {state.plan_file}")

    return 0


def cmd_decisions(args) -> int:
    """Show decisions made during orchestration."""
    decision_files = get_decisions()

    if not decision_files:
        print("No decisions recorded yet.")
        return 0

    print("Decisions made during orchestration:")
    print("=" * 40)

    for decision_file in decision_files:
        print(f"\n{decision_file.name}:")
        print("-" * 40)
        print(decision_file.read_text())

    return 0


def cmd_clean(args) -> int:
    """Delete the .dvx/ directory in current working directory."""
    import shutil

    dvx_dir = get_dvx_dir()

    if not dvx_dir.exists():
        print("No .dvx/ directory to clean.")
        return 0

    shutil.rmtree(dvx_dir)
    print(f"Removed {dvx_dir}")
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
    status_parser = subparsers.add_parser("status", help="Show current status")
    status_parser.set_defaults(func=cmd_status)

    # decisions
    decisions_parser = subparsers.add_parser("decisions", help="Show decisions made")
    decisions_parser.set_defaults(func=cmd_decisions)

    # clean
    clean_parser = subparsers.add_parser("clean", help="Delete .dvx/ directory")
    clean_parser.set_defaults(func=cmd_clean)

    # plan
    plan_parser = subparsers.add_parser("plan", help="Generate or update a plan file with Claude ultrathink")
    plan_parser.add_argument("plan_file", nargs="?", help="Path to PLAN-*.md file (optional)")
    plan_parser.set_defaults(func=cmd_plan)

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
