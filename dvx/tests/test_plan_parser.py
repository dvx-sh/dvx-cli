"""Tests for plan_parser module."""

import shutil
import tempfile
from pathlib import Path

import pytest

from plan_parser import TaskStatus, get_next_pending_task, parse_plan

# Skip tests that require Claude CLI if it's not available
requires_claude = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="Claude CLI not available"
)


@requires_claude
def test_parse_checkbox_tasks():
    """Test parsing checkbox-style tasks."""
    content = """# Test Plan

## Tasks

- [ ] **1.1** First task
  Some description

- [x] **1.2** Completed task

- [ ] **1.3** Third task
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write(content)
        f.flush()

        tasks = parse_plan(f.name)

        assert len(tasks) == 3
        assert tasks[0].id == "1.1"
        assert tasks[0].title == "First task"
        assert tasks[0].status == TaskStatus.PENDING

        assert tasks[1].id == "1.2"
        assert tasks[1].status == TaskStatus.DONE

        assert tasks[2].id == "1.3"
        assert tasks[2].status == TaskStatus.PENDING

        Path(f.name).unlink()


@requires_claude
def test_get_next_pending_task():
    """Test getting the next pending task."""
    content = """# Test Plan

- [x] **1** Done
- [ ] **2** Pending
- [ ] **3** Also pending
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        f.write(content)
        f.flush()

        task = get_next_pending_task(f.name)

        assert task is not None
        assert task.id == "2"
        assert task.title == "Pending"

        Path(f.name).unlink()
