"""
Plan file parser using Claude for intelligent parsing.

Uses Claude Code to understand any plan format and extract tasks.
Status is tracked separately to avoid modifying the plan file.
"""

import json
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional
import logging

from claude_session import run_claude

logger = logging.getLogger(__name__)

# Cache directory for parsed plans
CACHE_DIR = Path(__file__).parent.parent / ".cache"

# Status tracking file (in .dvx directory of the project)
STATUS_FILE = ".dvx/task-status.json"


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
    line_number: int  # For updating the file (approximate for AI-parsed plans)


def _get_cache_path(filepath: Path) -> Path:
    """Get cache file path for a plan file."""
    CACHE_DIR.mkdir(exist_ok=True)
    # Hash the filepath and content for cache key
    content = filepath.read_text()
    cache_key = hashlib.md5(f"{filepath}:{content}".encode()).hexdigest()
    return CACHE_DIR / f"plan-{cache_key}.json"


def _get_status_file() -> Path:
    """Get the status tracking file path."""
    return Path(STATUS_FILE)


def _load_status_overrides() -> dict[str, str]:
    """Load status overrides from the tracking file."""
    status_file = _get_status_file()
    if not status_file.exists():
        return {}
    try:
        data = json.loads(status_file.read_text())
        return data.get('status', {})
    except Exception as e:
        logger.warning(f"Failed to load status file: {e}")
        return {}


def _save_status_override(task_id: str, status: TaskStatus) -> None:
    """Save a status override to the tracking file."""
    status_file = _get_status_file()

    # Ensure .dvx directory exists
    status_file.parent.mkdir(exist_ok=True)

    # Load existing
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text())
        except Exception:
            data = {}
    else:
        data = {}

    if 'status' not in data:
        data['status'] = {}

    data['status'][task_id] = status.value
    status_file.write_text(json.dumps(data, indent=2))
    logger.debug(f"Saved status override: {task_id} -> {status.value}")


def _load_from_cache(filepath: Path) -> Optional[list[Task]]:
    """Load parsed tasks from cache if available and fresh."""
    cache_path = _get_cache_path(filepath)
    if not cache_path.exists():
        return None

    try:
        data = json.loads(cache_path.read_text())
        tasks = []
        for t in data['tasks']:
            tasks.append(Task(
                id=t['id'],
                title=t['title'],
                description=t['description'],
                status=TaskStatus(t['status']),
                line_number=t['line_number'],
            ))
        logger.debug(f"Loaded {len(tasks)} tasks from cache")
        return tasks
    except Exception as e:
        logger.warning(f"Failed to load cache: {e}")
        return None


def _save_to_cache(filepath: Path, tasks: list[Task]) -> None:
    """Save parsed tasks to cache."""
    cache_path = _get_cache_path(filepath)
    data = {
        'tasks': [
            {
                'id': t.id,
                'title': t.title,
                'description': t.description,
                'status': t.status.value,
                'line_number': t.line_number,
            }
            for t in tasks
        ]
    }
    cache_path.write_text(json.dumps(data, indent=2))
    logger.debug(f"Saved {len(tasks)} tasks to cache")


def _parse_with_claude(filepath: Path) -> list[Task]:
    """Use Claude to parse the plan file."""
    content = filepath.read_text()

    prompt = f"""Analyze this plan file and extract all tasks/phases/steps that need to be implemented.

PLAN FILE CONTENT:
---
{content}
---

Extract each distinct task/phase/step that represents a unit of work to be implemented.
For each task, determine:
1. A unique ID (use phase numbers like "1", "2", "3" or "1.1", "1.2" if nested)
2. A short title (the main heading or summary)
3. A description (the details/requirements for that task)
4. Status: "pending" (not started), "in_progress", "done", or "blocked"
   - Look for markers like [x], [DONE], [IN_PROGRESS], [BLOCKED], checkboxes, etc.
   - If no status marker, assume "pending"

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "tasks": [
    {{
      "id": "1",
      "title": "Short task title",
      "description": "Detailed description of what needs to be done",
      "status": "pending",
      "line_number": 10
    }}
  ]
}}

Important:
- Extract actionable implementation tasks, not just documentation sections
- If the plan has numbered phases (Phase 1, Phase 2, etc.), treat each phase as a task
- Include enough description for an implementer to understand what to do
- line_number should be approximate (the line where the task heading appears)
- Return an empty tasks array if no actionable tasks are found
"""

    logger.info(f"Parsing plan with Claude: {filepath}")
    result = run_claude(prompt, timeout=120)

    if not result.success:
        logger.error(f"Claude parsing failed: {result.output}")
        raise RuntimeError(f"Failed to parse plan with Claude: {result.block_reason or 'unknown error'}")

    # Parse the JSON response
    output = result.output.strip()

    # Try to extract JSON from the output (Claude might add some text)
    json_start = output.find('{')
    json_end = output.rfind('}') + 1
    if json_start >= 0 and json_end > json_start:
        output = output[json_start:json_end]

    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse Claude's JSON response: {e}")
        logger.error(f"Raw output: {output[:500]}")
        raise RuntimeError(f"Claude returned invalid JSON: {e}")

    tasks = []
    for t in data.get('tasks', []):
        status_str = t.get('status', 'pending').lower()
        try:
            status = TaskStatus(status_str)
        except ValueError:
            status = TaskStatus.PENDING

        tasks.append(Task(
            id=str(t.get('id', len(tasks) + 1)),
            title=t.get('title', 'Untitled'),
            description=t.get('description', ''),
            status=status,
            line_number=t.get('line_number', 0),
        ))

    logger.info(f"Claude extracted {len(tasks)} tasks from {filepath}")
    return tasks


def _apply_status_overrides(tasks: list[Task]) -> list[Task]:
    """Apply status overrides from tracking file to tasks."""
    overrides = _load_status_overrides()
    if not overrides:
        return tasks

    for task in tasks:
        if task.id in overrides:
            try:
                task.status = TaskStatus(overrides[task.id])
            except ValueError:
                pass

    return tasks


def parse_plan(filepath: str | Path) -> list[Task]:
    """
    Parse a PLAN file and extract tasks using Claude.

    Uses caching to avoid repeated Claude calls for unchanged plans.
    Applies status overrides from tracking file.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Plan file not found: {filepath}")

    # Try cache first
    cached = _load_from_cache(filepath)
    if cached is not None:
        logger.info(f"Using cached parse: {len(cached)} tasks from {filepath}")
        return _apply_status_overrides(cached)

    # Parse with Claude
    tasks = _parse_with_claude(filepath)

    # Cache the result
    _save_to_cache(filepath, tasks)

    # Apply any status overrides
    return _apply_status_overrides(tasks)


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
    Update a task's status.

    Status is tracked in a separate file (.dvx/task-status.json) rather than
    modifying the plan file, for speed and simplicity.
    """
    _save_status_override(task_id, new_status)
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


def clear_cache() -> None:
    """Clear all cached plan parses."""
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("plan-*.json"):
            f.unlink()
        logger.info("Cleared plan cache")


def clear_status() -> None:
    """Clear all status overrides."""
    status_file = _get_status_file()
    if status_file.exists():
        status_file.unlink()
        logger.info("Cleared status overrides")
