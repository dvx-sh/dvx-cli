"""Tests for plan_parser module."""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from plan_parser import (
    TaskStatus,
    _save_status_override,
    get_next_pending_task,
    parse_plan,
)

# Skip tests that require Claude CLI if it's not available
requires_claude = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="Claude CLI not available"
)


@requires_claude
def test_parse_checkbox_tasks():
    """
    Test parsing checkbox-style tasks.

    NOTE: Task status is determined by the status tracking file, NOT by
    checkbox markers in the plan. This prevents Claude from mistakenly
    marking tasks as done when they haven't been implemented.

    All tasks default to PENDING unless explicitly marked DONE in the
    status tracking file.
    """
    content = """# Test Plan

## Tasks

- [ ] **1.1** First task
  Some description

- [x] **1.2** Completed task

- [ ] **1.3** Third task
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)

        try:
            plan_file = Path(tmpdir) / "test_plan.md"
            plan_file.write_text(content)

            tasks = parse_plan(str(plan_file))

            assert len(tasks) == 3
            assert tasks[0].id == "1.1"
            assert tasks[0].title == "First task"
            # All tasks are PENDING by default (status file is source of truth)
            assert tasks[0].status == TaskStatus.PENDING
            assert tasks[1].status == TaskStatus.PENDING  # [x] in plan doesn't matter
            assert tasks[2].status == TaskStatus.PENDING

            # Now mark task 1.2 as DONE in the status tracking file
            Path(".dvx").mkdir(exist_ok=True)
            _save_status_override(plan_file, "1.2", TaskStatus.DONE)

            # Re-parse and verify the override is applied
            tasks = parse_plan(str(plan_file))
            assert tasks[0].status == TaskStatus.PENDING
            assert tasks[1].status == TaskStatus.DONE  # Now DONE via override
            assert tasks[2].status == TaskStatus.PENDING

        finally:
            os.chdir(original_cwd)


@requires_claude
def test_get_next_pending_task():
    """
    Test getting the next pending task.

    Task completion is tracked via the status tracking file, not the plan file.
    """
    content = """# Test Plan

- [x] **1** Done
- [ ] **2** Pending
- [ ] **3** Also pending
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_cwd = os.getcwd()
        os.chdir(tmpdir)

        try:
            plan_file = Path(tmpdir) / "test_plan.md"
            plan_file.write_text(content)

            # Without status overrides, all tasks are PENDING
            task = get_next_pending_task(str(plan_file))
            assert task is not None
            assert task.id == "1"  # First task (all are pending)

            # Mark task 1 as DONE in the status tracking file
            Path(".dvx").mkdir(exist_ok=True)
            _save_status_override(plan_file, "1", TaskStatus.DONE)

            # Now the next pending task should be task 2
            task = get_next_pending_task(str(plan_file))
            assert task is not None
            assert task.id == "2"
            assert task.title == "Pending"

        finally:
            os.chdir(original_cwd)
