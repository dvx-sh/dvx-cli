"""
Plan file parser using Claude for intelligent parsing.

Uses Claude Code to understand any plan format and extract tasks.
Status is tracked separately to avoid modifying the plan file.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from claude_session import run_claude

logger = logging.getLogger(__name__)

# Cache directory for parsed plans
CACHE_DIR = Path(__file__).parent.parent / ".cache"

# Status tracking file (in .dvx directory of the project)
STATUS_FILE = ".dvx/task-status.json"

# Token limit for plan files (Claude's context window consideration)
# Plan files exceeding this will be compressed automatically
MAX_PLAN_TOKENS = 20000  # Conservative limit to leave room for prompts

# Rough token estimation: ~4 chars per token for English text
CHARS_PER_TOKEN = 4


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


def _load_status_overrides(plan_filepath: Path) -> dict[str, str]:
    """Load status overrides from the tracking file for a specific plan."""
    status_file = _get_status_file()
    if not status_file.exists():
        return {}
    try:
        data = json.loads(status_file.read_text())
        plan_key = plan_filepath.name
        return data.get(plan_key, {})
    except Exception as e:
        logger.warning(f"Failed to load status file: {e}")
        return {}


def _save_status_override(plan_filepath: Path, task_id: str, status: TaskStatus) -> None:
    """Save a status override to the tracking file for a specific plan."""
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

    plan_key = plan_filepath.name
    if plan_key not in data:
        data[plan_key] = {}

    data[plan_key][task_id] = status.value
    status_file.write_text(json.dumps(data, indent=2))
    logger.debug(f"Saved status override: {plan_key}:{task_id} -> {status.value}")


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


def _estimate_tokens(text: str) -> int:
    """Rough token estimate for text."""
    return len(text) // CHARS_PER_TOKEN


def _compress_plan_file(filepath: Path) -> Path:
    """
    Compress an oversized plan file by extracting only incomplete work.

    When a plan file grows too large (from verbose implementation notes on
    completed tasks), this function:
    1. Reads the status tracking file to know which tasks are done
    2. Backs up the original first
    3. Uses Claude to create a lean version with only pending tasks
    4. Validates the result

    Returns the path to the compressed file (same as input filepath).
    """
    original_content = filepath.read_text()
    tokens = _estimate_tokens(original_content)

    logger.warning(f"Plan file too large: {filepath.name} (~{tokens} tokens, limit {MAX_PLAN_TOKENS})")
    logger.info("Compressing plan file to keep only incomplete work...")

    # Back up original FIRST (before Claude might modify it)
    backup_path = filepath.with_suffix('.md.backup')
    backup_path.write_text(original_content)
    logger.info(f"Backed up original to: {backup_path}")

    # Load status overrides to know which tasks are done
    overrides = _load_status_overrides(filepath)
    done_tasks = [tid for tid, status in overrides.items() if status == "done"]

    prompt = f"""This plan file has grown too large and needs to be compressed.

TASK STATUS (from tracking file):
Done tasks: {', '.join(done_tasks) if done_tasks else 'none'}

CURRENT PLAN FILE:
---
{original_content}
---

Create a COMPRESSED version that:
1. Keeps the title and essential context (first ~50 lines or less)
2. For DONE tasks: Keep ONLY the task ID and title with [x] marker, remove all implementation notes
3. For PENDING/IN-PROGRESS tasks: Keep full description needed for implementation
4. Remove verbose implementation notes, file lists, and patterns from completed work

Write the compressed content directly to the plan file: {filepath}

Example of compressed done task:
### 3.2.1 [x] Convert get_ontology tool to error results

Example of pending task (keep full details):
### 4. [ ] Document the error handling pattern
   Full description of what needs to be done...

IMPORTANT: Preserve task IDs exactly as they appear (1, 2, 3.1, 3.2.1, etc.)

After writing the compressed file, commit it with a message like:
  plan: compress {filepath.name} to remove completed task notes

This commit prevents the next task from seeing a large deletion diff and blocking.
"""

    result = run_claude(prompt, timeout=180)

    if not result.success:
        # Restore backup on failure
        filepath.write_text(original_content)
        logger.error(f"Failed to compress plan: {result.block_reason}")
        raise RuntimeError(f"Plan compression failed: {result.block_reason}")

    # Re-read file to see what Claude wrote (it may have used Write tool)
    compressed = filepath.read_text()

    # Validate we got something reasonable
    if len(compressed) < 100:
        # Restore backup
        filepath.write_text(original_content)
        raise RuntimeError("Compressed plan is too small - something went wrong")

    # Check it's actually smaller
    if len(compressed) >= len(original_content):
        logger.warning("Compressed plan is not smaller than original - keeping as-is")
        filepath.write_text(original_content)
        return filepath

    new_tokens = _estimate_tokens(compressed)
    if new_tokens > MAX_PLAN_TOKENS:
        logger.warning(f"Compressed plan still large (~{new_tokens} tokens), but proceeding")

    reduction = (1 - len(compressed) / len(original_content)) * 100
    logger.info(f"Compressed plan: {len(original_content)} -> {len(compressed)} chars ({reduction:.0f}% reduction)")

    # Clear cache since content changed
    cache_path = _get_cache_path(filepath)
    if cache_path.exists():
        cache_path.unlink()

    return filepath


def _parse_with_claude(filepath: Path) -> list[Task]:
    """Use Claude to parse the plan file."""
    content = filepath.read_text()

    # Check if plan file is too large and needs compression
    tokens = _estimate_tokens(content)
    if tokens > MAX_PLAN_TOKENS:
        _compress_plan_file(filepath)
        content = filepath.read_text()  # Re-read compressed content

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


def _apply_status_overrides(tasks: list[Task], plan_filepath: Path) -> list[Task]:
    """
    Apply status overrides from tracking file to tasks.

    IMPORTANT: The status tracking file (.dvx/task-status.json) is the source
    of truth for task completion. Tasks NOT in the override file are reset to
    PENDING, regardless of what status Claude assigned during parsing.

    This prevents the finalizer from being called too soon when Claude
    mistakenly marks unimplemented tasks as "done" (e.g., by misinterpreting
    [x] markers in the plan file).
    """
    overrides = _load_status_overrides(plan_filepath)

    for task in tasks:
        if task.id in overrides:
            # Task has an explicit status override - use it
            try:
                task.status = TaskStatus(overrides[task.id])
            except ValueError:
                task.status = TaskStatus.PENDING
        else:
            # Task NOT in overrides - reset to PENDING
            # This ensures Claude's parsing status doesn't incorrectly mark
            # tasks as done when they haven't been implemented
            task.status = TaskStatus.PENDING

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
        return _apply_status_overrides(cached, filepath)

    # Parse with Claude
    tasks = _parse_with_claude(filepath)

    # Cache the result
    _save_to_cache(filepath, tasks)

    # Apply any status overrides
    return _apply_status_overrides(tasks, filepath)


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
    filepath = Path(filepath)
    _save_status_override(filepath, task_id, new_status)
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


def clear_status_for_plan(filepath: str | Path) -> None:
    """
    Clear status overrides for a specific plan.

    Simply removes this plan's entry from the status file,
    leaving other plans' statuses intact.
    """
    filepath = Path(filepath)
    status_file = _get_status_file()

    if not status_file.exists():
        return

    try:
        data = json.loads(status_file.read_text())
        plan_key = filepath.name
        if plan_key in data:
            del data[plan_key]
            status_file.write_text(json.dumps(data, indent=2))
            logger.info(f"Cleared status for {plan_key}")
    except Exception as e:
        logger.warning(f"Failed to clear plan status: {e}")


def sync_plan_state(filepath: str | Path) -> dict:
    """
    Synchronize task status with the plan file's markers.

    This is the "planner" that runs at the start of each `dvx run` to ensure
    the status tracking file matches reality. It reads the plan file, has Claude
    parse the [x] markers, and updates the status tracking file accordingly.

    This handles cases where:
    - The user manually updated the plan file
    - The escalator completed multiple tasks in an interactive session
    - `dvx clean` was run but status file wasn't properly cleared

    Returns:
        dict with sync results: {synced, added, removed, tasks}
    """
    filepath = Path(filepath)
    logger.info(f"Syncing plan state for: {filepath}")

    # Force a fresh parse by clearing the cache
    cache_path = _get_cache_path(filepath)
    if cache_path.exists():
        cache_path.unlink()
        logger.debug("Cleared cache for fresh parse")

    # Parse the plan file fresh - Claude will read the [x] markers
    tasks = _parse_with_claude(filepath)

    # Save to cache for future use
    _save_to_cache(filepath, tasks)

    # Load current status overrides for this plan
    current_statuses = _load_status_overrides(filepath)

    # Sync: Update status file to match what Claude parsed from plan
    synced = 0
    added = 0
    removed = 0

    task_ids = {t.id for t in tasks}

    for task in tasks:
        current = current_statuses.get(task.id)

        # If Claude found [x] (done) in the plan but we have pending/in_progress
        if task.status == TaskStatus.DONE:
            if current != TaskStatus.DONE.value:
                _save_status_override(filepath, task.id, TaskStatus.DONE)
                if current is None:
                    added += 1
                    logger.info(f"Added done status for task {task.id} from plan markers")
                else:
                    synced += 1
                    logger.info(f"Synced task {task.id} to done (was {current})")

        # If Claude found [ ] (pending) and we have done, trust the plan
        elif task.status == TaskStatus.PENDING:
            if current == TaskStatus.DONE.value:
                _save_status_override(filepath, task.id, TaskStatus.PENDING)
                synced += 1
                logger.info(f"Synced task {task.id} to pending (was done)")

        # Keep in_progress as is (don't override active work)

    # Remove statuses for tasks that no longer exist in the plan
    for task_id in list(current_statuses.keys()):
        if task_id not in task_ids:
            # Task was removed from plan - clean up status
            status_file = _get_status_file()
            if status_file.exists():
                try:
                    data = json.loads(status_file.read_text())
                    plan_key = filepath.name
                    if plan_key in data and task_id in data[plan_key]:
                        del data[plan_key][task_id]
                        status_file.write_text(json.dumps(data, indent=2))
                        removed += 1
                        logger.info(f"Removed status for deleted task {task_id}")
                except Exception:
                    pass

    result = {
        'synced': synced,
        'added': added,
        'removed': removed,
        'tasks': len(tasks),
    }

    logger.info(f"Plan sync complete: {synced} synced, {added} added, {removed} removed")
    return result
