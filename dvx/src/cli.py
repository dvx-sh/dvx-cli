#!/usr/bin/env python3
"""
dvx - Development Orchestrator CLI.

Automates the implement → review → test → commit development loop.
"""

import argparse
import sys
import os
import logging
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from state import (
    load_state, reset_state, get_decisions, clear_blocked,
    get_dvx_dir, Phase,
)
from plan_parser import get_plan_summary, get_next_pending_task
from orchestrator import run_orchestrator

# Configure logging
def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    format_str = "%(asctime)s [%(levelname)s] %(message)s" if verbose else "%(message)s"

    logging.basicConfig(
        level=level,
        format=format_str,
        handlers=[logging.StreamHandler()],
    )

    # Also log to file
    dvx_dir = get_dvx_dir()
    if dvx_dir.exists():
        file_handler = logging.FileHandler(dvx_dir / "dvx.log")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logging.getLogger().addHandler(file_handler)


def cmd_start(args) -> int:
    """Start processing a plan file."""
    plan_file = Path(args.plan_file)

    if not plan_file.exists():
        print(f"Error: Plan file not found: {plan_file}")
        return 1

    # Check for existing state
    state = load_state()
    if state and not args.force:
        print(f"Error: Already processing {state.plan_file}")
        print(f"  Current phase: {state.phase}")
        print(f"  Current task: {state.current_task_title or 'none'}")
        print()
        print("Use 'dvx status' to see details, 'dvx continue' to resume,")
        print("or 'dvx start --force' to restart from scratch.")
        return 1

    print(f"Starting orchestration of: {plan_file}")
    print()

    # Show plan summary
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

    print(f"Starting with task {next_task.id}: {next_task.title}")
    print("=" * 60)
    print()

    return run_orchestrator(str(plan_file))


def cmd_status(args) -> int:
    """Show current orchestration status."""
    state = load_state()

    if state is None:
        print("No active orchestration.")
        print("Use 'dvx start <plan-file>' to begin.")
        return 0

    print("DVX Status")
    print("=" * 40)
    print(f"Plan file: {state.plan_file}")
    print(f"Phase: {state.phase}")
    print(f"Current task: {state.current_task_id or 'none'} - {state.current_task_title or ''}")
    print(f"Iteration: {state.iteration_count}/{state.max_iterations}")
    print(f"Started: {state.started_at}")
    print(f"Updated: {state.updated_at}")
    print()

    if state.phase == Phase.BLOCKED.value:
        blocked_file = get_dvx_dir() / "blocked-context.md"
        if blocked_file.exists():
            print("BLOCKED - See .dvx/blocked-context.md for details")
            print("Run 'dvx continue' after resolving the issue.")
    else:
        # Show plan summary
        try:
            summary = get_plan_summary(state.plan_file)
            print(f"Progress: {summary['done']}/{summary['total']} tasks complete")
        except FileNotFoundError:
            print(f"Warning: Plan file not found: {state.plan_file}")

    return 0


def cmd_continue(args) -> int:
    """Continue after human intervention."""
    state = load_state()

    if state is None:
        print("No active orchestration to continue.")
        print("Use 'dvx start <plan-file>' to begin.")
        return 1

    if state.phase == Phase.BLOCKED.value:
        print("Clearing blocked state and continuing...")
        clear_blocked()

    print(f"Resuming orchestration of: {state.plan_file}")
    print()

    return run_orchestrator(state.plan_file)


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


def cmd_stop(args) -> int:
    """Stop orchestration gracefully."""
    state = load_state()

    if state is None:
        print("No active orchestration.")
        return 0

    print(f"Stopping orchestration of: {state.plan_file}")
    print(f"Current task: {state.current_task_title}")
    print()

    if args.force:
        reset_state()
        print("State cleared. Orchestration stopped.")
    else:
        print("Current task will complete, then orchestration will pause.")
        print("Use 'dvx stop --force' to immediately clear state.")
        # Set a flag to stop after current task (would need to implement)
        # For now, just inform the user

    return 0


def cmd_reset(args) -> int:
    """Reset orchestration state."""
    state = load_state()

    if state is None:
        print("No state to reset.")
        return 0

    if not args.force:
        print(f"This will clear all state for: {state.plan_file}")
        response = input("Are you sure? [y/N] ")
        if response.lower() != 'y':
            print("Cancelled.")
            return 0

    reset_state()
    print("State reset. Use 'dvx start' to begin fresh.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dvx",
        description="Development Orchestrator - Automated implement/review/test/commit cycles",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # start
    start_parser = subparsers.add_parser("start", help="Start processing a plan file")
    start_parser.add_argument("plan_file", help="Path to PLAN-*.md file")
    start_parser.add_argument("-f", "--force", action="store_true", help="Force restart even if already running")
    start_parser.set_defaults(func=cmd_start)

    # status
    status_parser = subparsers.add_parser("status", help="Show current status")
    status_parser.set_defaults(func=cmd_status)

    # continue
    continue_parser = subparsers.add_parser("continue", help="Continue after human intervention")
    continue_parser.set_defaults(func=cmd_continue)

    # decisions
    decisions_parser = subparsers.add_parser("decisions", help="Show decisions made")
    decisions_parser.set_defaults(func=cmd_decisions)

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop orchestration")
    stop_parser.add_argument("-f", "--force", action="store_true", help="Force immediate stop")
    stop_parser.set_defaults(func=cmd_stop)

    # reset
    reset_parser = subparsers.add_parser("reset", help="Reset orchestration state")
    reset_parser.add_argument("-f", "--force", action="store_true", help="Skip confirmation")
    reset_parser.set_defaults(func=cmd_reset)

    # help
    help_parser = subparsers.add_parser("help", help="Show this help message")
    help_parser.set_defaults(func=lambda _: parser.print_help() or 0)

    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
