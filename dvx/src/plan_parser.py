"""
Plan file parser.

Parses PLAN-*.md files to extract tasks and their status.
Supports flexible markdown formats.
"""

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


@dataclass
class Task:
    """A task from the plan file."""
    id: str
    title: str
    description: str
    status: TaskStatus
    line_number: int  # For updating the file


def parse_plan(filepath: str | Path) -> list[Task]:
    """
    Parse a PLAN file and extract tasks.

    Supports formats:
    - [ ] Task description
    - [x] Completed task
    - **1.1** Task title (with - [ ] prefix)
    - ## Task N: Title
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Plan file not found: {filepath}")

    content = filepath.read_text()
    lines = content.split('\n')
    tasks: list[Task] = []

    # Patterns for task detection
    checkbox_pattern = re.compile(r'^(\s*)-\s*\[([ xX])\]\s*\*?\*?(\d+\.?\d*\.?\d*)?\*?\*?\s*(.*)')
    header_pattern = re.compile(r'^##\s+Task\s+(\d+):\s*(.*)', re.IGNORECASE)

    current_task_id = None
    current_description_lines: list[str] = []

    for i, line in enumerate(lines):
        # Check for checkbox-style tasks
        checkbox_match = checkbox_pattern.match(line)
        if checkbox_match:
            # Save previous task's description
            if current_task_id is not None and tasks:
                tasks[-1].description = '\n'.join(current_description_lines).strip()
                current_description_lines = []

            indent, check, task_num, title = checkbox_match.groups()
            title = title.strip()

            # Determine status
            if check.lower() == 'x':
                status = TaskStatus.DONE
            elif '[IN_PROGRESS]' in title.upper() or '[IN PROGRESS]' in title.upper():
                status = TaskStatus.IN_PROGRESS
                title = re.sub(r'\s*\[IN[_ ]PROGRESS\]\s*', '', title, flags=re.IGNORECASE)
            elif '[BLOCKED]' in title.upper():
                status = TaskStatus.BLOCKED
                title = re.sub(r'\s*\[BLOCKED\]\s*', '', title, flags=re.IGNORECASE)
            else:
                status = TaskStatus.PENDING

            # Generate task ID
            if task_num:
                task_id = task_num.rstrip('.')
            else:
                task_id = str(len(tasks) + 1)

            current_task_id = task_id

            tasks.append(Task(
                id=task_id,
                title=title,
                description='',
                status=status,
                line_number=i + 1,  # 1-indexed
            ))
            continue

        # Check for header-style tasks
        header_match = header_pattern.match(line)
        if header_match:
            if current_task_id is not None and tasks:
                tasks[-1].description = '\n'.join(current_description_lines).strip()
                current_description_lines = []

            task_num, title = header_match.groups()
            current_task_id = task_num

            # Look for status markers
            status = TaskStatus.PENDING
            if '[DONE]' in title.upper() or '[X]' in title:
                status = TaskStatus.DONE
                title = re.sub(r'\s*\[(DONE|X)\]\s*', '', title, flags=re.IGNORECASE)
            elif '[IN_PROGRESS]' in title.upper():
                status = TaskStatus.IN_PROGRESS
                title = re.sub(r'\s*\[IN_PROGRESS\]\s*', '', title, flags=re.IGNORECASE)

            tasks.append(Task(
                id=task_num,
                title=title.strip(),
                description='',
                status=status,
                line_number=i + 1,
            ))
            continue

        # Collect description lines for current task
        if current_task_id is not None:
            # Stop collecting if we hit a new section
            if line.startswith('## ') or line.startswith('### '):
                if tasks:
                    tasks[-1].description = '\n'.join(current_description_lines).strip()
                current_description_lines = []
                current_task_id = None
            else:
                current_description_lines.append(line)

    # Don't forget the last task's description
    if current_task_id is not None and tasks:
        tasks[-1].description = '\n'.join(current_description_lines).strip()

    logger.info(f"Parsed {len(tasks)} tasks from {filepath}")
    return tasks


def get_next_pending_task(filepath: str | Path) -> Optional[Task]:
    """Get the next pending (not done, not blocked) task."""
    tasks = parse_plan(filepath)

    # First look for in_progress tasks
    for task in tasks:
        if task.status == TaskStatus.IN_PROGRESS:
            return task

    # Then look for pending tasks
    for task in tasks:
        if task.status == TaskStatus.PENDING:
            return task

    return None


def update_task_status(filepath: str | Path, task_id: str, new_status: TaskStatus) -> None:
    """
    Update a task's status in the plan file.

    Modifies the checkbox or adds a status marker.
    """
    filepath = Path(filepath)
    content = filepath.read_text()
    lines = content.split('\n')

    # Find the task by ID and update its status
    checkbox_pattern = re.compile(r'^(\s*-\s*\[)([ xX])(\]\s*\*?\*?)(' + re.escape(task_id) + r'\.?\*?\*?\s*)(.*)')

    for i, line in enumerate(lines):
        match = checkbox_pattern.match(line)
        if match:
            prefix, _check, middle, task_num, rest = match.groups()

            # Remove any existing status markers from rest
            rest = re.sub(r'\s*\[(IN_PROGRESS|BLOCKED|DONE)\]\s*', '', rest, flags=re.IGNORECASE)

            if new_status == TaskStatus.DONE:
                new_check = 'x'
                status_marker = ''
            elif new_status == TaskStatus.IN_PROGRESS:
                new_check = ' '
                status_marker = ' [IN_PROGRESS]'
            elif new_status == TaskStatus.BLOCKED:
                new_check = ' '
                status_marker = ' [BLOCKED]'
            else:
                new_check = ' '
                status_marker = ''

            lines[i] = f"{prefix}{new_check}{middle}{task_num}{rest}{status_marker}"
            break

    filepath.write_text('\n'.join(lines))
    logger.info(f"Updated task {task_id} to {new_status.value}")


def get_plan_summary(filepath: str | Path) -> dict:
    """Get a summary of plan status."""
    tasks = parse_plan(filepath)

    return {
        'total': len(tasks),
        'done': sum(1 for t in tasks if t.status == TaskStatus.DONE),
        'in_progress': sum(1 for t in tasks if t.status == TaskStatus.IN_PROGRESS),
        'pending': sum(1 for t in tasks if t.status == TaskStatus.PENDING),
        'blocked': sum(1 for t in tasks if t.status == TaskStatus.BLOCKED),
        'tasks': tasks,
    }
