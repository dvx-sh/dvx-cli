"""
State management for dvx orchestrator.

Manages the .dvx/{plan}/ directory structure for tracking orchestration state.
Supports multiple concurrent plans in the same project.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DVX_DIR = ".dvx"
STATE_FILE = "state.json"
BLOCKED_FILE = "blocked-context.md"
LOG_FILE = "dvx.log"


def get_plan_dir_name(plan_file: str) -> str:
    """Get the directory name for a plan file (just the filename, no path)."""
    return Path(plan_file).name


class Phase(Enum):
    IDLE = "idle"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    TESTING = "testing"
    COMMITTING = "committing"
    FINALIZING = "finalizing"  # Final review before merge
    BLOCKED = "blocked"
    PAUSED = "paused"  # Step mode: paused after task completion
    COMPLETE = "complete"


@dataclass
class State:
    """Orchestrator state for a project."""
    plan_file: str
    current_task_id: Optional[str] = None
    current_task_title: Optional[str] = None
    phase: str = "idle"
    overseer_session_id: Optional[str] = None
    iteration_count: int = 0
    max_iterations: int = 3
    step_mode: bool = False  # If True, pause after each task completion
    started_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(**data)


def get_dvx_root(project_dir: Optional[str] = None) -> Path:
    """Get the root .dvx directory for a project."""
    project_dir = Path(project_dir or os.getcwd())
    return project_dir / DVX_DIR


def get_dvx_dir(plan_file: Optional[str] = None, project_dir: Optional[str] = None) -> Path:
    """
    Get the .dvx directory for a plan.

    If plan_file is provided, returns .dvx/{plan_file}/
    Otherwise returns the root .dvx/ directory.
    """
    root = get_dvx_root(project_dir)
    if plan_file:
        return root / get_plan_dir_name(plan_file)
    return root


def ensure_dvx_dir(plan_file: Optional[str] = None, project_dir: Optional[str] = None) -> Path:
    """Ensure .dvx directory (and plan subdirectory if specified) exists."""
    root = get_dvx_root(project_dir)
    root.mkdir(exist_ok=True)

    # Create .gitignore in root if it doesn't exist
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("# Ignore all dvx working files\n*\n!.gitignore\n")

    if plan_file:
        plan_dir = root / get_plan_dir_name(plan_file)
        plan_dir.mkdir(exist_ok=True)
        return plan_dir

    return root


def load_state(plan_file: str, project_dir: Optional[str] = None) -> Optional[State]:
    """Load state from .dvx/{plan}/state.json."""
    dvx_dir = get_dvx_dir(plan_file, project_dir)
    state_file = dvx_dir / STATE_FILE

    if not state_file.exists():
        return None

    try:
        data = json.loads(state_file.read_text())
        return State.from_dict(data)
    except Exception as e:
        logger.error(f"Error loading state: {e}")
        return None


def save_state(state: State, project_dir: Optional[str] = None) -> None:
    """Save state to .dvx/{plan}/state.json."""
    dvx_dir = ensure_dvx_dir(state.plan_file, project_dir)
    state_file = dvx_dir / STATE_FILE

    state.updated_at = datetime.now().isoformat()
    state_file.write_text(json.dumps(state.to_dict(), indent=2))
    logger.debug(f"Saved state to {state_file}")


def reset_state(plan_file: str, project_dir: Optional[str] = None) -> None:
    """Remove state file to reset."""
    dvx_dir = get_dvx_dir(plan_file, project_dir)
    state_file = dvx_dir / STATE_FILE

    if state_file.exists():
        state_file.unlink()
        logger.info("State reset")


def create_initial_state(plan_file: str, project_dir: Optional[str] = None) -> State:
    """Create initial state for a new orchestration run."""
    state = State(
        plan_file=plan_file,
        phase=Phase.IDLE.value,
        started_at=datetime.now().isoformat(),
    )
    save_state(state, project_dir)
    return state


def update_phase(phase: Phase, plan_file: str, project_dir: Optional[str] = None) -> State:
    """Update the current phase."""
    state = load_state(plan_file, project_dir)
    if state is None:
        raise RuntimeError("No active state - run 'dvx run <plan>' first")

    state.phase = phase.value
    save_state(state, project_dir)
    return state


def set_current_task(task_id: str, task_title: str, plan_file: str, project_dir: Optional[str] = None) -> State:
    """Set the current task being worked on."""
    state = load_state(plan_file, project_dir)
    if state is None:
        raise RuntimeError("No active state - run 'dvx run <plan>' first")

    state.current_task_id = task_id
    state.current_task_title = task_title
    state.iteration_count = 0
    save_state(state, project_dir)
    return state


def increment_iteration(plan_file: str, project_dir: Optional[str] = None) -> tuple[State, bool]:
    """
    Increment iteration count and check if max reached.

    Returns: (state, exceeded_max)
    """
    state = load_state(plan_file, project_dir)
    if state is None:
        raise RuntimeError("No active state - run 'dvx run <plan>' first")

    state.iteration_count += 1
    exceeded = state.iteration_count > state.max_iterations
    save_state(state, project_dir)
    return state, exceeded


def set_overseer_session(session_id: str, plan_file: str, project_dir: Optional[str] = None) -> State:
    """Store the overseer session ID for resumption."""
    state = load_state(plan_file, project_dir)
    if state is None:
        raise RuntimeError("No active state - run 'dvx run <plan>' first")

    state.overseer_session_id = session_id
    save_state(state, project_dir)
    return state


def write_blocked_context(reason: str, context: str, plan_file: str, session_id: Optional[str] = None, project_dir: Optional[str] = None) -> Path:
    """Write blocked context file for human review."""
    dvx_dir = ensure_dvx_dir(plan_file, project_dir)
    blocked_file = dvx_dir / BLOCKED_FILE

    content = f"""# Blocked: {reason}

**Time**: {datetime.now().isoformat()}
**Session ID**: {session_id or 'unknown'}

## Context

{context}

## Instructions

Run `dvx run {plan_file}` and when resolved type `/exit` - dvx will continue.

"""
    blocked_file.write_text(content)
    logger.info(f"Wrote blocked context to {blocked_file}")
    return blocked_file


def clear_blocked(plan_file: str, project_dir: Optional[str] = None) -> None:
    """Clear the blocked state and file."""
    dvx_dir = get_dvx_dir(plan_file, project_dir)
    blocked_file = dvx_dir / BLOCKED_FILE

    if blocked_file.exists():
        blocked_file.unlink()

    state = load_state(plan_file, project_dir)
    if state and state.phase == Phase.BLOCKED.value:
        state.phase = Phase.IDLE.value
        save_state(state, project_dir)


def log_decision(topic: str, decision: str, reasoning: str, alternatives: list[str], plan_file: str, project_dir: Optional[str] = None) -> None:
    """
    Log a decision to DECISIONS-{topic}.md.

    These are decisions made by Claude that the user should review.
    """
    dvx_dir = ensure_dvx_dir(plan_file, project_dir)
    decision_file = dvx_dir / f"DECISIONS-{topic}.md"

    # Append to existing or create new
    timestamp = datetime.now().isoformat()

    entry = f"""
## Decision at {timestamp}

**Decision**: {decision}

**Reasoning**: {reasoning}

**Alternatives considered**:
"""
    for alt in alternatives:
        entry += f"- {alt}\n"

    entry += "\n---\n"

    if decision_file.exists():
        current = decision_file.read_text()
        decision_file.write_text(current + entry)
    else:
        header = f"# Decisions: {topic}\n\nDecisions made during automated development.\n\n---\n"
        decision_file.write_text(header + entry)

    logger.info(f"Logged decision to {decision_file}")


def get_decisions(plan_file: str, project_dir: Optional[str] = None) -> list[Path]:
    """Get all decision files in .dvx/{plan}/."""
    dvx_dir = get_dvx_dir(plan_file, project_dir)
    if not dvx_dir.exists():
        return []

    return list(dvx_dir.glob("DECISIONS-*.md"))
