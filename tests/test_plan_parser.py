"""Tests for plan_parser module."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import plan_parser
from claude_session import SessionResult
from plan_parser import (
    TaskStatus,
    _parse_with_claude,
    _save_status_override,
    get_next_pending_task,
    parse_plan,
)

# Skip tests that require Claude CLI if it's not available
requires_claude = pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="Claude CLI not available"
)


def test_parse_with_claude_prompt_defines_tasks(monkeypatch):
    """
    The extraction prompt must explicitly state what counts as a task and what
    does not, so Claude returns a consistent task count between start and finish.
    """
    captured = {}

    def fake_run_claude(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return SessionResult(
            output=json.dumps({"tasks": []}),
            session_id=None,
            success=True,
        )

    monkeypatch.setattr(plan_parser, "run_claude", fake_run_claude)

    with tempfile.TemporaryDirectory() as tmpdir:
        plan_file = Path(tmpdir) / "test_plan.md"
        plan_file.write_text("# Plan\n\n## Tasks\n\n- [ ] Do the thing\n")

        _parse_with_claude(plan_file)

    prompt = captured["prompt"]
    # Explicit inclusion rules
    assert "WHAT COUNTS AS A TASK" in prompt
    assert "- [ ]" in prompt and "- [x]" in prompt
    assert "Implementation Tasks" in prompt
    assert "Implementation Steps" in prompt
    assert "Phases" in prompt
    # Explicit exclusion rules
    assert "WHAT IS NOT A TASK" in prompt
    assert "Files to Modify" in prompt
    assert "Testing" in prompt and "Verification" in prompt
    # Status derivation from markers
    assert "[IN_PROGRESS]" in prompt
    assert "[BLOCKED]" in prompt
    # Output contract
    assert "ONLY valid JSON" in prompt


def test_parse_with_claude_derives_status_from_markers(monkeypatch):
    """Status from the Claude response is mapped onto Task objects."""
    response = {
        "tasks": [
            {"id": "1", "title": "A", "description": "", "status": "done"},
            {"id": "2", "title": "B", "description": "", "status": "pending"},
            {"id": "3", "title": "C", "description": "", "status": "in_progress"},
            {"id": "4", "title": "D", "description": "", "status": "blocked"},
        ]
    }

    def fake_run_claude(prompt, *args, **kwargs):
        return SessionResult(
            output=json.dumps(response),
            session_id=None,
            success=True,
        )

    monkeypatch.setattr(plan_parser, "run_claude", fake_run_claude)

    with tempfile.TemporaryDirectory() as tmpdir:
        plan_file = Path(tmpdir) / "test_plan.md"
        plan_file.write_text("# Plan\n")

        tasks = _parse_with_claude(plan_file)

    assert [t.status for t in tasks] == [
        TaskStatus.DONE,
        TaskStatus.PENDING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
    ]


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
